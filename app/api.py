"""REST API for historical Dota 2 match analytics."""

import os
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, FastAPI, Request

from app.history.domain import HistoricalMatch
from app.storage import SQLiteRepository


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "autopilot.db"
router = APIRouter(prefix="/api")


def get_repository(request: Request) -> SQLiteRepository:
    return cast(SQLiteRepository, request.app.state.repository)


@router.get("/matches", response_model=list[HistoricalMatch])
def list_matches(
    repository: Annotated[SQLiteRepository, Depends(get_repository)],
) -> list[HistoricalMatch]:
    return repository.list_historical_matches()


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Create an application using the existing SQLite schema and repository."""
    selected_path = (
        Path(db_path)
        if db_path is not None
        else Path(os.environ.get("DOTA_ANALYTICS_DB_PATH", DEFAULT_DB_PATH))
    )
    application = FastAPI(
        title="Dota 2 Live Analytics & Prediction Platform",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.repository = SQLiteRepository(selected_path)
    application.include_router(router)
    return application
