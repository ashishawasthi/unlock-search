"""
FastAPI application factory. The ONE place the HTTP surface is assembled. It is
byte-identical across profiles: it builds a Container from UNLOCK_PROFILE, migrates +
seeds via the store port, and mounts the same routes + SPA. Retargeting is one env var.

Run: UNLOCK_PROFILE=local uvicorn core.api.app:create_app --factory --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.container import Container
from core.domain.seed import seed

UI = Path(__file__).resolve().parent.parent.parent / "ui"


def create_app() -> FastAPI:
    c = Container()
    c.store().migrate()
    seed(c.store())

    app = FastAPI(title="gcp-unlock")
    app.state.c = c

    from core.api.routes import access, admin, auth, chat, documents, search
    for module in (auth, documents, search, access, chat, admin):
        app.include_router(module.router)

    @app.get("/")
    def index():
        return FileResponse(UI / "index.html")

    if UI.exists():
        app.mount("/static", StaticFiles(directory=str(UI)), name="static")
    return app
