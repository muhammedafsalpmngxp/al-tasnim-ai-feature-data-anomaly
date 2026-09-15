"""Central configuration, loaded once from .env.

Every tunable in this system lives here and nowhere else. In particular, NO TABLE OR COLUMN
NAME appears anywhere in this codebase: which schemas, tables and columns the engine may see is
decided entirely by ALLOWED_SCHEMAS / EXCLUDED_TABLES / EXCLUDED_COLUMNS below, so pointing the
app at a different database is a config change, never a code change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    """Comma-separated setting, lowercased and blank-stripped."""
    return tuple(s.strip().lower() for s in _get(name, default).split(",") if s.strip())



_DEFAULT_SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 40.0, "high": 20.0, "medium": 8.0, "low": 5.0,
}


def _severity_weights(name: str) -> tuple[tuple[str, float], ...]:
    """Parse "critical:40,high:20,medium:8,low:5" over the defaults.

    Returned as a tuple of pairs rather than a dict: Settings is a frozen dataclass and a dict
    default would be shared mutable state across every instance.
    """
    weights = dict(_DEFAULT_SEVERITY_WEIGHTS)
    raw = _get(name, "")
    for part in raw.split(","):
        key, _, value = part.partition(":")
        key = key.strip().lower()
        if not key:
            continue
        try:
            weights[key] = float(value)
        except ValueError:
            # Keep the default for this severity. Silently zeroing it would make every finding
            # at that severity cost nothing, and the score would look like an improvement.
            continue
    return tuple(weights.items())

@dataclass(frozen=True)
class Settings:
    # -- LLM ---------------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: _get("LLM_PROVIDER", "openai"))
    llm_model: str = field(default_factory=lambda: _get("LLM_MODEL", "qwen3.6:35b-a3b"))
    ollama_base_url: str = field(
        default_factory=lambda: _get("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    ollama_num_ctx: int = field(default_factory=lambda: _get_int("OLLAMA_NUM_CTX", 32768))
    openai_api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _get("OPENAI_MODEL", "gpt-5-mini"))
    # Optional cheap tier. Blank = use the main model, i.e. enabling it is purely opt-in and
    # leaving it unset can never break a node.
    ollama_fast_model: str = field(default_factory=lambda: _get("OLLAMA_FAST_MODEL"))
    openai_fast_model: str = field(default_factory=lambda: _get("OPENAI_FAST_MODEL"))

    llm_timeout: int = field(default_factory=lambda: _get_int("LLM_TIMEOUT", 180))
    llm_max_retries: int = field(default_factory=lambda: _get_int("LLM_MAX_RETRIES", 2))

    @property
    def _is_openai(self) -> bool:
        return self.llm_provider.lower() in ("open", "openai")

    @property
    def active_model(self) -> str:
        """The model a normal (reasoning-tier) call will use."""
        return self.openai_model if self._is_openai else self.llm_model

    @property
    def fast_model(self) -> str:
        """The cheap tier; falls back to the main model when none is configured."""
        if self._is_openai:
            return self.openai_fast_model or self.openai_model
        return self.ollama_fast_model or self.llm_model

    # -- Database ----------------------------------------------------------------
    db_server: str = field(default_factory=lambda: _get("DB_SERVER"))
    db_port: int = field(default_factory=lambda: _get_int("DB_PORT", 1433))
    db_name: str = field(default_factory=lambda: _get("DB_NAME"))
    db_user: str = field(default_factory=lambda: _get("DB_USER"))
    db_password: str = field(default_factory=lambda: _get("DB_PASSWORD"))
    db_driver: str = field(default_factory=lambda: _get("DB_DRIVER"))
    db_encrypt: str = field(default_factory=lambda: _get("DB_ENCRYPT", "yes"))
    db_trust_cert: str = field(default_factory=lambda: _get("DB_TRUST_CERT", "yes"))

    # -- Table scope -------------------------------------------------------------
    # The ONLY control over what the engine can see. Deliberately wider than a chatbot's scope:
    # more tables means more declared foreign keys to check for orphans, and more pairs of
    # sources that are supposed to agree with each other.
    allowed_schemas: tuple[str, ...] = field(
        default_factory=lambda: _csv("ALLOWED_SCHEMAS", "dbo")
    )
    excluded_tables: tuple[str, ...] = field(default_factory=lambda: _csv("EXCLUDED_TABLES"))
    excluded_columns: tuple[str, ...] = field(default_factory=lambda: _csv("EXCLUDED_COLUMNS"))

    # -- Row-volume control ------------------------------------------------------
    # FOUR separate caps because they solve four different problems. `sample_rows` is the only
    # one that bounds an LLM prompt; the others bound a FILE, and a file has no context window.
    sample_rows: int = field(default_factory=lambda: _get_int("ANOMALY_SAMPLE_ROWS", 25))
    report_rows: int = field(default_factory=lambda: _get_int("ANOMALY_REPORT_ROWS", 2000))
    export_max_rows: int = field(
        default_factory=lambda: _get_int("ANOMALY_EXPORT_MAX_ROWS", 1_000_000)
    )
    fetch_batch: int = field(default_factory=lambda: _get_int("ANOMALY_FETCH_BATCH", 5000))

    # -- Query execution ---------------------------------------------------------
    query_timeout: int = field(default_factory=lambda: _get_int("ANOMALY_QUERY_TIMEOUT", 60))
    detail_timeout: int = field(default_factory=lambda: _get_int("ANOMALY_DETAIL_TIMEOUT", 600))
    run_concurrency: int = field(
        default_factory=lambda: max(1, _get_int("ANOMALY_RUN_CONCURRENCY", 3))
    )
    compile_concurrency: int = field(
        default_factory=lambda: max(1, _get_int("ANOMALY_COMPILE_CONCURRENCY", 1))
    )
    read_uncommitted: bool = field(
        default_factory=lambda: _get_bool("ANOMALY_READ_UNCOMMITTED", True)
    )

    # -- Agentic guardrails ------------------------------------------------------
    # Two budgets, never one. max_sql_retries funds MECHANICAL failures (validator rejection, DB
    # error, contract violation); verify_retries funds SEMANTIC ones. Sharing a single counter
    # lets a couple of syntax errors early in a rule leave the Verifier with zero rewrites - it
    # then rejects and is immediately overruled, which is the worst of both behaviours.
    max_sql_retries: int = field(default_factory=lambda: _get_int("MAX_SQL_RETRIES", 2))
    verify_retries: int = field(default_factory=lambda: _get_int("VERIFY_RETRIES", 1))
    # How many members of one structural family may be sent to the SQL Author before the family
    # is written off. More than one because each member names DIFFERENT tables and columns, so a
    # second attempt is a genuinely different query rather than a retry of a rejected one - and
    # a single rejected representative used to take every sibling with it (50 probes lost in one
    # compile). Bounded, because a family this database cannot express at all must not cost one
    # call per feature to discover that.
    family_author_attempts: int = field(
        default_factory=lambda: max(1, _get_int("ANOMALY_FAMILY_AUTHOR_ATTEMPTS", 3))
    )
    max_compile_calls: int = field(
        default_factory=lambda: _get_int("ANOMALY_MAX_COMPILE_CALLS", 200)
    )
    schema_prune_above_tables: int = field(
        default_factory=lambda: _get_int("ANOMALY_SCHEMA_PRUNE_ABOVE_TABLES", 60)
    )
    # How much a finding of each severity moves the headline score. A BUSINESS judgement, so it
    # is configurable rather than fixed in Python: "critical:40,high:20,medium:8,low:5".
    # Malformed entries are ignored and the default for that severity stands, because a typo
    # here must never silently make a whole severity weightless - which would hide findings.
    # A text column counts as "really a quantity" once at least this share of its values parse
    # as numbers; below it the column is codes, labels or free text and no cast probe is built.
    # MEASURED, never guessed from the column name. Measured distribution on AlTasnimBI: every
    # column is either <= 32% (codes) or >= 95% (numeric), with one at 71% - so the default sits
    # in that gap. Lower it to include a borderline column, raise it to be stricter.
    text_numeric_min_parse_rate: float = field(
        default_factory=lambda: _get_float("ANOMALY_TEXT_NUMERIC_MIN_PARSE", 0.80)
    )
    severity_weights: tuple[tuple[str, float], ...] = field(
        default_factory=lambda: _severity_weights("ANOMALY_SEVERITY_WEIGHTS")
    )
    auto_compile: bool = field(default_factory=lambda: _get_bool("ANOMALY_AUTO_COMPILE", True))
    # How many outdated probes a detection run may rebuild by itself before it stops and asks.
    # Auto-compile exists for DRIFT - a handful of probes left behind by a column rename. A
    # schema-wide change invalidates everything at once, and silently spending an hour of LLM
    # calls inside what the operator asked to be a run is not a decision this should make alone.
    auto_compile_max_rules: int = field(
        default_factory=lambda: _get_int("ANOMALY_AUTO_COMPILE_MAX_RULES", 25)
    )

    # -- Observability -----------------------------------------------------------
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))

    # -- HTTP API ----------------------------------------------------------------
    api_host: str = field(default_factory=lambda: _get("API_HOST", "0.0.0.0"))
    # 8100 by default so this can run alongside the chatbot on 8000.
    api_port: int = field(default_factory=lambda: _get_int("API_PORT", 8100))
    api_reload: bool = field(default_factory=lambda: _get_bool("API_RELOAD", False))
    api_workers: int = field(default_factory=lambda: max(1, _get_int("API_WORKERS", 1)))
    api_key: str = field(default_factory=lambda: _get("API_KEY"))
    cors_origins: str = field(default_factory=lambda: _get("CORS_ORIGINS", "*"))

    # -- Langfuse tracing (optional) ---------------------------------------------
    # Blank public key => no handler is attached anywhere and every code path is identical to
    # the pre-Langfuse behaviour. Nothing has to be reverted to turn tracing off.
    langfuse_base_url: str = field(default_factory=lambda: _get("LANGFUSE_BASE_URL"))
    langfuse_public_key: str = field(default_factory=lambda: _get("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: str = field(default_factory=lambda: _get("LANGFUSE_SECRET_KEY"))


settings = Settings()
