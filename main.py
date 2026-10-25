"""Compatibility entrypoint.

Many platforms (Render, Railway, Fly, older Heroku setups) default to
``main:app``. The real application lives in :mod:`app.main`; this shim only
re-exports it so either import path works.

    uvicorn main:app        # equivalent
    uvicorn app.main:app    # canonical
"""

from app.main import app, create_app

__all__ = ["app", "create_app"]
