"""DYOTAK FastAPI application factory and lifespan manager.

One origin serving static frontend + /api/* endpoints.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    copilot,
    eval as eval_router,
    health,
    jobs,
    layers,
    preflight,
    presets,
    report,
    tiles,
    upstream,
)
from app.common.errors import DyotakError
from app.jobs.registry import job_registry
from app.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle management."""
    # Recover any interrupted jobs from previous runs
    job_registry.reload_and_recover()
    yield


def create_app() -> FastAPI:
    """Create and configure the DYOTAK FastAPI application."""
    app = FastAPI(
        title="DYOTAK",
        description="Orbital intelligence for ground-level survival — Satellite flood-damage mapping system for rescue teams.",
        version=settings.app_version,
        lifespan=lifespan
    )

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Typed domain error handler
    @app.exception_handler(DyotakError)
    async def dyotak_error_handler(request: Request, exc: DyotakError):
        return JSONResponse(
            status_code=400,
            content={"error": exc.to_detail().model_dump()}
        )

    # Include API routers under /api
    app.include_router(health.router, prefix="/api")
    app.include_router(presets.router, prefix="/api")
    app.include_router(preflight.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(layers.router, prefix="/api")
    app.include_router(upstream.router, prefix="/api")
    app.include_router(copilot.router, prefix="/api")
    app.include_router(report.router, prefix="/api")
    app.include_router(tiles.router, prefix="/api")
    app.include_router(eval_router.router, prefix="/api")

    # Static frontend mount if built
    frontend_dist = Path("frontend/dist")
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="frontend")

    return app


app = create_app()
