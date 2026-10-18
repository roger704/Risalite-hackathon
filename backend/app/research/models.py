"""Nebius chat models and schema-validated single calls (review, writer)."""

from __future__ import annotations

import json
import os
import re
import time
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.domain.errors import ConfigError
from app.research.config import ResearchConfig
from app.research.events import Events, clean_error

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
FORBIDDEN_MODEL = "kimi"

M = TypeVar("M", bound=BaseModel)


def chat_model(name: str, cfg: ResearchConfig) -> BaseChatModel:
    if FORBIDDEN_MODEL in name.lower():
        raise ConfigError("Kimi models are excluded for research (E8)")
    if cfg.model_factory is not None:
        return cfg.model_factory(name)
    api_key = os.environ.get("NEBIUS_API_KEY")
    if not api_key:
        raise ConfigError("NEBIUS_API_KEY is not set")
    from langchain_nebius import ChatNebius

    return ChatNebius(
        model=name,
        api_key=api_key,
        base_url=os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL),
        temperature=0,
        timeout=min(cfg.timeout_s, 120.0),
        max_retries=2,
    )


def model_name(model: BaseChatModel, fallback: str) -> str:
    return str(getattr(model, "model_name", None) or getattr(model, "model", None) or fallback)


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, dict))
        )
    return ""


def usage(message: BaseMessage) -> tuple[int | None, int | None]:
    meta = getattr(message, "usage_metadata", None) or {}
    return meta.get("input_tokens"), meta.get("output_tokens")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str, schema: type[M]) -> M | None:
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            return schema.model_validate(json.loads(candidate[start : end + 1]))
        except (ValueError, ValidationError):
            continue
    return None


async def structured_call(
    model: BaseChatModel,
    name: str,
    system: str,
    user: str,
    schema: type[M],
    events: Events,
    why: str,
) -> M | None:
    """One JSON call validated against ``schema``; ``None`` (plus a tool_error) on failure."""
    instructions = (
        f"{system}\n\nRespond with one JSON object matching this JSON schema and nothing "
        f"else:\n{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )
    messages = [SystemMessage(content=instructions), HumanMessage(content=user)]
    for attempt in range(2):
        started = time.perf_counter()
        try:
            reply = await model.ainvoke(messages)
        except Exception as error:
            await events("tool_error", why=why, detail={"tool": "llm", "error": clean_error(error)})
            return None
        tokens_in, tokens_out = usage(reply)
        await events(
            "model_call",
            why=why,
            detail={"attempt": attempt + 1},
            model=model_name(model, name),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            seconds=time.perf_counter() - started,
        )
        parsed = parse_json(message_text(reply), schema)
        if parsed is not None:
            return parsed
        messages = [
            *messages,
            reply if isinstance(reply, AIMessage) else AIMessage(content=message_text(reply)),
            HumanMessage(content="That was not valid JSON for the schema. Return only the JSON."),
        ]
    await events(
        "tool_error", why=why, detail={"tool": "llm", "error": "invalid structured output"}
    )
    return None
