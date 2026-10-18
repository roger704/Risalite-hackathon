"""Event payloads for the orchestrator's emit callback (§9.1 shape without seq/ts/run_id)."""

from __future__ import annotations

import os
from inspect import isawaitable
from typing import Any

from app.research.config import Emit

SECRET_ENV = ("TAVILY_API_KEY", "NEBIUS_API_KEY")


def clean_error(error: BaseException) -> str:
    message = str(error)[:500]
    for name in SECRET_ENV:
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{type(error).__name__}: {message}"


class Events:
    def __init__(self, emit: Emit, agent: str, model: str | None = None):
        self._emit = emit
        self.agent = agent
        self.model = model

    async def __call__(
        self,
        type: str,
        *,
        why: str | None = None,
        detail: dict[str, Any] | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        seconds: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {"agent": self.agent, "why": why, "detail": detail or {}}
        extras = {
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "seconds": round(seconds, 3) if seconds is not None else None,
        }
        payload.update({key: value for key, value in extras.items() if value is not None})
        result = self._emit(type, payload)
        if isawaitable(result):
            await result
