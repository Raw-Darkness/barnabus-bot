"""World lore: loaded from a text file, hot-reloaded when it changes, served by /lore."""
import logging
import os
import re

import discord
from discord import app_commands

from . import core
from .llm import chat_async, llm_enabled, llm_unavailable, utility_model

LORE = ""
_mtime: float = 0.0


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        logging.exception("Failed to read %s", path)
        return ""


def load_lore() -> None:
    global LORE, _mtime
    path = core.config.get("LorePath", "world_lore.txt")
    if os.path.exists(path):
        LORE = read_text(path)
        _mtime = os.path.getmtime(path)
    else:
        LORE, _mtime = "", 0.0
    logging.info("Loaded lore: %d chars (%s)", len(LORE), path)


def lore_changed() -> bool:
    path = core.config.get("LorePath", "world_lore.txt")
    return os.path.exists(path) and os.path.getmtime(path) > _mtime


def query_words(q: str) -> set[str]:
    """Words plus naive singular forms so "drakes" still matches "drake"."""
    words = set(re.findall(r"[a-z']+", q))
    extra = set()
    for w in words:
        base = re.sub(r"'s$", "", w)
        if base != w:
            extra.add(base)
        for stem in (base, w):
            if stem.endswith("es") and len(stem) > 5:
                extra.add(stem[:-2])
            if stem.endswith("s") and len(stem) > 4:
                extra.add(stem[:-1])
    return words | extra


@core.tree.command(name="lore", description="Ask about the world's lore — the answer is shown only to you")
@app_commands.describe(topic="What do you want to know about?")
async def lore_command(interaction: discord.Interaction, topic: str):
    try:
        if not LORE:
            await interaction.response.send_message("I have no lore to share.", ephemeral=True)
            return
        if not llm_enabled():
            await llm_unavailable(interaction)
            return
        from .faq import ask_bucket
        if not ask_bucket(interaction.user.id).consume():
            await interaction.response.send_message(core.text("MsgRateLimited", "One at a time. The forge doesn't rush."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        persona = (core.config.get("Personality") or "").strip()
        system = (
            f"You are {core.bot_name()}. Answer the question about the world using ONLY the lore below — never invent facts. "
            "Stay in character and keep it under 200 words; substance first, flavor second. "
            "If the lore does not cover the topic, say so in character in one or two lines.\n\n"
            + (f"Character:\n{persona[:1500]}\n\n" if persona else "")
            + "World lore:\n" + LORE
        )
        try:
            reply = await chat_async([{"role": "system", "content": system}, {"role": "user", "content": topic.strip()[:500]}],
                                     temperature=0.5, max_tokens=450, model=core.config.get("LoreModel") or utility_model())
        except Exception:
            logging.exception("/lore LLM call failed")
            reply = None
        reply = (reply or "").strip() or core.text("MsgLoreNoAnswer", "Nothing in the ledger about that. Ask something else.")
        await interaction.followup.send(reply[:1900], ephemeral=True)
    except Exception:
        logging.exception("/lore failed")
