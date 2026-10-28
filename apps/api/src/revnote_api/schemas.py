"""Response models for the scaffold endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Body of ``GET /health`` — process liveness only."""

    status: str = Field(pattern="^ok$")
    service: str
    version: str
    environment: str


class RootResponse(BaseModel):
    """Body of ``GET /`` — hello placeholder."""

    service: str
    message: str
    docs: str = "/docs"
    health: str = "/health"
