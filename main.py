"""Entry point for ``uvicorn main:app`` (used by Dockerfile / Render)."""

from webhook_app import app

__all__ = ["app"]
