"""HTTP API for the Sentinel -- a thin, read-mostly layer over the existing engine.

Nothing in `app/sentinel`, `app/db`, `app/reporting` or `app/cli.py` is changed by this
package. Every endpoint either reads through `FindingsStore` (exactly what `cli.py show`
already does) or starts the same `Orchestrator.run()` the CLI's `run` command calls, on a
background thread so the request can return immediately and the frontend can poll progress.
"""
