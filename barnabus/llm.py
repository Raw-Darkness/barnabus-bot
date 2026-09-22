"""OpenRouter client. Every model call in the bot goes through chat_async()."""
import asyncio
import logging

import discord
from openai import AsyncOpenAI

from . import core

_client: AsyncOpenAI | None = None
_client_key: tuple[str, str] | None = None


def client() -> AsyncOpenAI:
    """Rebuilt when the endpoint or key changes in the config."""
    global _client, _client_key
    key = (str(core.config.get("OpenAPIEndpoint") or "https://openrouter.ai/api/v1"), str(core.config.get("OpenAPIKey") or ""))
    if _client is None or key != _client_key:
        _client = AsyncOpenAI(base_url=key[0], api_key=key[1])
        _client_key = key
    return _client


def utility_model() -> str | None:
    return core.config.get("UtilityModel") or None


def llm_enabled() -> bool:
    """Master switch. False = no model calls at all: /ask, /lore, Translate, FAQ
    auto-answers and digests go quiet; every moderation feature keeps running."""
    return bool(core.config.get("LLMEnabled", True))


async def llm_unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        core.text("LLMDisabledNotice", "The forge is cold for that. Not today."), ephemeral=True)


class LLMResponseError(Exception):
    """200 with no usable completion (provider error, content flag) — not worth retrying."""


async def chat_async(messages: list[dict[str, str]], _retries: int = 3, **kwargs) -> str | None:
    if not llm_enabled():
        raise LLMResponseError("LLM calls are disabled (LLMEnabled=false)")
    model = kwargs.pop("model", None) or core.config["OpenaiModel"]
    kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "reasoning": {"enabled": False}}
    last: Exception | None = None
    for attempt in range(1, _retries + 1):
        try:
            resp = await client().chat.completions.create(messages=messages, model=model, **kwargs)
            if not getattr(resp, "choices", None):
                extra = getattr(resp, "model_extra", None) or {}
                err = extra.get("error") if isinstance(extra, dict) else None
                logging.error("LLM returned no choices. error=%r", err)
                raise LLMResponseError(str(err or "no choices returned"))
            return resp.choices[0].message.content
        except LLMResponseError:
            raise
        except Exception as e:
            last = e
            if attempt < _retries:
                wait = 2 ** attempt
                logging.warning("LLM call failed (attempt %d/%d): %s — retrying in %ds", attempt, _retries, e, wait)
                await asyncio.sleep(wait)
    raise last  # type: ignore[misc]
