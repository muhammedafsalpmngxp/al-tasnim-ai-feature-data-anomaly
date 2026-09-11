"""Configuration, loaded from the repo-root .env.

The Sentinel deliberately uses its own DQ_* scope variables rather than the chat feature's
ALLOWED_SCHEMAS / EXCLUDED_TABLES: those hide four tables this feature must read
(dbo.activity_master_mapping is required by business rule §3, and the weightage tables live
in dbo.activity_task_plan / dbo.activity_taskplan_job_progress). See docs/02-FEATURE-PLAN.md §10.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _csv(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


class Settings(BaseSettings):
    """Every value is overridable from .env; the defaults are the measured-safe ones."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- source database (READ ONLY: BIuser is db_datareader) -------------------
    db_server: str = Field(..., alias="DB_SERVER")
    db_port: int = Field(1433, alias="DB_PORT")
    db_name: str = Field(..., alias="DB_NAME")
    db_user: str = Field(..., alias="DB_USER")
    db_password: str = Field(..., alias="DB_PASSWORD")

    db_connect_timeout: int = Field(30, alias="DQ_DB_CONNECT_TIMEOUT")
    db_query_timeout: int = Field(300, alias="DQ_DB_QUERY_TIMEOUT")
    db_maxdop: int = Field(2, alias="DQ_DB_MAXDOP")

    # ---- Sentinel scope --------------------------------------------------------
    dq_allowed_schemas: Annotated[list[str], NoDecode] = Field(
        default=["dbo", "dsq", "project", "ref", "well", "core", "bridge", "wbs"],
        alias="DQ_ALLOWED_SCHEMAS",
    )
    dq_excluded_tables: Annotated[list[str], NoDecode] = Field(
        default=["dbo.sysdiagrams", "well.well_details", "well.wmr_conversion"],
        alias="DQ_EXCLUDED_TABLES",
    )
    dq_excluded_columns: Annotated[list[str], NoDecode] = Field(
        default=[], alias="DQ_EXCLUDED_COLUMNS"
    )

    # Tables above this row count are sampled rather than fully scanned.
    dq_large_table_row_limit: int = Field(2_000_000, alias="DQ_LARGE_TABLE_ROW_LIMIT")
    dq_sample_percent: int = Field(2, alias="DQ_SAMPLE_PERCENT")

    # ---- findings store (SQLite: the source account cannot write) --------------
    dq_store: str = Field("sqlite", alias="DQ_STORE")
    dq_store_path: Path = Field(Path("./output/sentinel.db"), alias="DQ_STORE_PATH")
    dq_output_dir: Path = Field(Path("./output"), alias="DQ_OUTPUT_DIR")

    # ---- LLM ------------------------------------------------------------------
    # OPENAI_MODEL is the single source of truth: whatever the user sets there is what
    # every LLM job uses, by default. The four DQ_LLM_MODEL_* vars below are OPTIONAL
    # per-job overrides (docs/05 -- narration is high-volume/low-reasoning and can run
    # cheaper; correlation/summary run once per report and benefit from a stronger
    # model) -- each is empty unless explicitly set, and `LLMClient._model_for()` falls
    # back to `openai_model` whenever the specific override is blank. No model name is
    # hardcoded: the one default below exists only so the app has something to run with
    # if NEITHER OPENAI_MODEL nor any override is set, and is exactly what OPENAI_MODEL
    # itself defaults to (there is no separate hardcoded literal anywhere else).
    llm_provider: str = Field("openai", alias="LLM_PROVIDER")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    dq_llm_enabled: bool = Field(True, alias="DQ_LLM_ENABLED")
    dq_llm_max_tokens_per_batch: int = Field(90_000, alias="DQ_LLM_MAX_TOKENS_PER_BATCH")
    dq_llm_temperature: float = Field(0.1, alias="DQ_LLM_TEMPERATURE")
    dq_llm_max_retries: int = Field(2, alias="DQ_LLM_MAX_RETRIES")
    dq_llm_timeout_seconds: int = Field(60, alias="DQ_LLM_TIMEOUT_SECONDS")
    # Optional per-job overrides, ALL BLANK BY DEFAULT. Blank means "use OPENAI_MODEL",
    # so out of the box exactly one model -- whatever `.env` sets -- runs every LLM job:
    # narrate, correlate, summarise and the suggestion agent. Explicit user instruction
    # (2026-09-09): do not silently run a job on a different model than OPENAI_MODEL.
    #
    # These exist only so ONE job can be pointed elsewhere deliberately, by setting the
    # variable. They previously defaulted CORRELATE/SUMMARY to gpt-5-mini, which meant a
    # run using gpt-4o-mini for narration still made two calls on a model the user never
    # asked for -- visible in the log as a 400 `llm.temperature_unsupported`, because
    # gpt-5-mini rejects the `temperature` parameter outright.
    dq_llm_model_narrate: str = Field("", alias="DQ_LLM_MODEL_NARRATE")
    dq_llm_model_agent: str = Field("", alias="DQ_LLM_MODEL_AGENT")
    dq_llm_model_correlate: str = Field("", alias="DQ_LLM_MODEL_CORRELATE")
    dq_llm_model_summary: str = Field("", alias="DQ_LLM_MODEL_SUMMARY")

    # ---- API (Phase 3; unused in Phase 1) --------------------------------------
    api_host: str = Field("0.0.0.0", alias="API_HOST")
    api_port: int = Field(8001, alias="API_PORT")
    api_cors_origins: Annotated[list[str], NoDecode] = Field(
        default=["http://localhost:5173"], alias="API_CORS_ORIGINS"
    )
    dq_max_concurrent_runs: int = Field(2, alias="DQ_MAX_CONCURRENT_RUNS")

    # ---- logging ---------------------------------------------------------------
    log_level: str = Field("INFO", alias="DQ_LOG_LEVEL")
    log_json: bool = Field(False, alias="DQ_LOG_JSON")

    @field_validator(
        "dq_allowed_schemas",
        "dq_excluded_tables",
        "dq_excluded_columns",
        "api_cors_origins",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v):  # noqa: ANN001, ANN206
        return _csv(v)

    @field_validator("dq_sample_percent")
    @classmethod
    def _sane_sample(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("DQ_SAMPLE_PERCENT must be between 1 and 100")
        return v

    # -- derived paths, resolved against the repo root so the CLI works anywhere --
    @property
    def store_path(self) -> Path:
        p = self.dq_store_path
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def output_dir(self) -> Path:
        p = self.dq_output_dir
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def config_dir(self) -> Path:
        return BACKEND_ROOT / "config"

    @property
    def cache_dir(self) -> Path:
        """The compile-time agent's knowledge base (schema.txt/hint_data.txt/
        fingerprints.json). Under BACKEND_ROOT like config_dir, not REPO_ROOT like
        output_dir/store_path -- this is generated build state next to the code that
        reads it, not a data product like a run's reports."""
        return BACKEND_ROOT / ".cache"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
