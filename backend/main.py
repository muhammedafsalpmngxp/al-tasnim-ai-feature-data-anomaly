"""CLI entry point.

    python main.py <command>        or        python -m app.cli <command>

Both routes reach the same place; this file exists so the project can be run without knowing
the module layout.
"""
from app.cli import main

if __name__ == "__main__":
    main()
