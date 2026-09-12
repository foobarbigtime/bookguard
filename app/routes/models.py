from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ConfirmationRequest(BaseModel):
    confirm: str = Field(min_length=1, max_length=64)


def require_confirmation(payload: ConfirmationRequest, expected: str) -> None:
    if payload.confirm != expected:
        raise HTTPException(
            status_code=400,
            detail=f"Explicit {expected} confirmation is required.",
        )
