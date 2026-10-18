"""Nebius chat-model construction for the research agents."""

import os
from typing import Literal

from langchain_nebius import ChatNebius

DEFAULT_MODEL = "MiniMaxAI/MiniMax-M3"
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


def nebius_chat_model(
    role: Literal["orchestrator", "researcher"], model: str | None = None
) -> ChatNebius:
    resolved_model = (
        model
        or os.getenv(f"NEBIUS_MODEL_{role.upper()}")
        or os.getenv("NEBIUS_MODEL")
        or DEFAULT_MODEL
    )
    return ChatNebius(
        model=resolved_model,
        base_url=os.getenv("NEBIUS_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.getenv("NEBIUS_API_KEY"),
        temperature=0,
    )
