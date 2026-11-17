"""Backwards-compatible entry point: `uvicorn gateway:app` keeps working.

The implementation lives in the middle_agentic package; prefer `magent serve`.
"""
from middle_agentic.server.app import create_app

app = create_app()

if __name__ == "__main__":
    from middle_agentic.cli import main

    raise SystemExit(main(["serve"]))
