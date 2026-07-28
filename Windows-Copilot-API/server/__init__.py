"""OpenAI-compatible HTTP server for Microsoft Copilot.

Start it:

    from server import app
    app()

(`python app.py` in the project root does exactly this.) The server runs on
http://127.0.0.1:8000 — set HOST / PORT to override. It bridges the OpenAI Chat
Completions shape onto :class:`copilot.CopilotClient`; sign in once first with
``python -m copilot login``.

Code is split by concern:

    config.py         constants
    schemas.py        pydantic request models
    prompt.py         flatten OpenAI messages -> one Copilot prompt
    openai_format.py  build OpenAI response/chunk shapes
    api.py            FastAPI app, routes, upstream serialization
"""

import os

from .api import app as _api


def app(host=None, port=None) -> None:
    """Start the server (blocks while uvicorn runs).

    On first run (no saved session) this opens a browser for interactive sign-in
    before serving, so requests don't fail with a "not signed in" error.
    """
    import uvicorn

    from copilot.auth import load_auth

    if host is None:
        host = os.environ.get("HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("PORT", "8000"))

    # Ensure a signed-in Copilot session exists before we start serving. On the
    # very first run this triggers the interactive browser sign-in (instead of
    # letting the first HTTP request fail), then caches it for reuse.
    try:
        load_auth()
    except Exception as exc:
        print(f"Warning: could not establish a Copilot session: {exc}")

    print(f"Copilot OpenAI-compatible API on http://{host}:{port}  (POST /v1/chat/completions)")
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": False,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO"},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }
    uvicorn.run(_api, host=host, port=port, log_config=log_config)


__all__ = ["app"]
