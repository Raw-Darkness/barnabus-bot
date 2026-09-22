"""Daily activity digest for the owner (and !summary on demand)."""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import discord

from . import core
from .llm import chat_async, llm_enabled, utility_model

LAST_RUN_PATH = ".summary_last_run"


def summary_tz() -> ZoneInfo:
    try:
        return ZoneInfo(str(core.config.get("SummaryTimezone", "Europe/Stockholm")))
    except Exception:
        return ZoneInfo("UTC")


async def _fetch(channel: discord.TextChannel, after: datetime) -> list[str]:
    lines: list[str] = []
    try:
        me = channel.guild.me
        if me is None or not channel.permissions_for(me).read_message_history:
            return []
        async for msg in channel.history(after=after, limit=int(core.config.get("SummaryMaxPerChannel", 500)), oldest_first=True):
            if msg.author.bot:
                continue
            c = (msg.content or "").strip()
            if c:
                lines.append(f"{msg.author.display_name}: {c[:300] + ('…' if len(c) > 300 else '')}")
    except discord.Forbidden:
        pass
    except Exception:
        logging.exception("Summary: failed to fetch #%s", channel.name)
    return lines


async def _summarize(channel_name: str, messages: list[str]) -> str:
    joined = "\n".join(messages)
    if len(joined) > 12000:
        joined = joined[:12000] + "\n[…truncated]"
    system = ("You are a concise server activity summarizer. Summarize the following Discord channel conversation. "
              "Focus on: key topics, decisions, questions asked, notable community interactions, general sentiment. "
              "Skip greetings, small talk and bot responses. Write 2-5 sentences; mention usernames when relevant.")
    try:
        out = await chat_async([{"role": "system", "content": system}, {"role": "user", "content": f"Channel: #{channel_name}\n\n{joined}"}],
                               temperature=0.3, max_tokens=300, model=utility_model())
        return (out or "").strip()
    except Exception:
        logging.exception("Summary: LLM failed for #%s", channel_name)
        return ""


async def _digest(parts: list[tuple[str, str, int]]) -> str:
    combined = "\n\n".join(f"#{n} ({c} messages):\n{s}" for n, s, c in parts)
    system = ("You are a server activity digest writer. Compile the per-channel summaries below into a clean daily "
              "briefing for a server owner. Group related topics across channels. Highlight anything that might need "
              "the owner's attention (complaints, questions directed at devs, heated discussions, bug reports, feature "
              "requests). End with a quick overall sentiment read. Under 800 words, markdown formatting.")
    try:
        out = await chat_async([{"role": "system", "content": system}, {"role": "user", "content": combined}],
                               temperature=0.3, max_tokens=1200, model=utility_model())
        return (out or "").strip() or combined
    except Exception:
        logging.exception("Summary: digest LLM failed")
        return combined


async def generate_server_summary(hours: int = 24) -> str | None:
    if not core.bot.guilds:
        return None
    after = datetime.now(timezone.utc) - timedelta(hours=hours)
    parts: list[tuple[str, str, int]] = []
    for guild in core.bot.guilds:
        for channel in guild.text_channels:
            msgs = await _fetch(channel, after)
            if len(msgs) < 3:
                continue
            s = await _summarize(channel.name, msgs)
            if s:
                parts.append((channel.name, s, len(msgs)))
    return await _digest(parts) if parts else None


def _read_last() -> float:
    try:
        with open(LAST_RUN_PATH) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def _write_last() -> None:
    try:
        with open(LAST_RUN_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception:
        logging.exception("Failed to write summary timestamp")


async def scheduler() -> None:
    await core.bot.wait_until_ready()
    while not core.bot.is_closed():
        try:
            tz = summary_tz()
            now = datetime.now(tz)
            target = now.replace(hour=core.cfg_int("SummaryHour", 8), minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            if not core.config.get("DailySummaryEnabled", True) or not llm_enabled():
                continue
            if time.time() - _read_last() < 72000:  # already sent within 20h (survives restarts)
                continue
            digest = await generate_server_summary(hours=24)
            if not digest:
                continue
            try:
                owner = await core.bot.fetch_user(core.owner_id())
                for chunk in core.chunks(digest):
                    await owner.send(chunk)
                _write_last()
            except discord.Forbidden:
                logging.error("Summary: cannot DM owner")
            except Exception:
                logging.exception("Summary: failed to send digest")
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("Summary scheduler error; retrying in 60s")
            await asyncio.sleep(60)
