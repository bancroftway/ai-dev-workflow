from __future__ import annotations

from pydantic import BaseModel


class SessionTitleResponse(BaseModel):
    title: str
