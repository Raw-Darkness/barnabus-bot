"""Starboard: enough ⭐/❤️ reactions repost a message to the highlights channel."""
import io
import logging
import time

import discord

from . import core
from .db import get_db


def _norm(e) -> str:
    return str(e).replace("️", "")


def _accepted() -> set[str]:
    raw = core.config.get("HighlightEmojis") or [core.config.get("HighlightEmoji", "⭐")]
    return {_norm(e) for e in raw}


def _matches(e, accepted: set[str]) -> bool:
    if _norm(e) in accepted:
        return True
    name = getattr(e, "name", None)
    return bool(name) and _norm(name) in accepted


def _image_attachment(msg: discord.Message):
    return next((a for a in msg.attachments if (a.content_type or "").startswith("image/")), None)


_in_flight: set[int] = set()


@core.bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        hl_id = core.cfg_int("HighlightsChannelID")
        if not hl_id or payload.guild_id is None or payload.channel_id == hl_id:
            return
        accepted = _accepted()
        if not _matches(payload.emoji, accepted):
            return
        channel = await core.get_channel(payload.channel_id)
        if channel is None:
            return
        msg = await channel.fetch_message(payload.message_id)
        att = _image_attachment(msg)
        # Bots' posts count only when listed (e.g. the image bot); everyone else's always.
        if msg.author.bot and msg.author.id not in core.cfg_ids("HighlightBotIDs"):
            return
        if core.config.get("HighlightImagesOnly", True) and att is None:
            return
        voters: set[int] = set()
        for reaction in msg.reactions:
            if _matches(reaction.emoji, accepted):
                async for u in reaction.users():
                    if u.id != core.bot.user.id:
                        voters.add(u.id)
        if len(voters) < int(core.config.get("HighlightThreshold", 3)) or msg.id in _in_flight:
            return
        if get_db().execute("SELECT 1 FROM highlights WHERE message_id = ?", (msg.id,)).fetchone():
            return
        _in_flight.add(msg.id)
        try:
            await _post(payload, msg, hl_id, len(voters), att)
        finally:
            _in_flight.discard(msg.id)
    except Exception:
        logging.exception("Highlight handler failed")


async def _post(payload, msg: discord.Message, hl_id: int, count: int, att) -> None:
    """Record only after the post succeeded, so a failed post stays eligible."""
    try:
        hl = await core.get_channel(hl_id)
        if hl is None:
            return
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        embed = discord.Embed(color=0xF1C40F, title=f"⭐ {count} · {msg.author.display_name}")
        files = []
        if att is not None:
            data = await att.read()
            files.append(discord.File(io.BytesIO(data), filename=att.filename))
            embed.set_image(url=f"attachment://{att.filename}")
            if msg.content:
                embed.description = msg.content[:300]
        else:
            embed.description = (msg.content or "")[:1000]
            embed.set_thumbnail(url=msg.author.display_avatar.url)
        embed.add_field(name="Source", value=f"[jump to message]({jump}) in <#{payload.channel_id}>")
        await hl.send(embed=embed, files=files)
    except discord.Forbidden:
        logging.error("Highlight: cannot post in %s — needs Send Messages, Embed Links, Attach Files.", hl_id)
        return
    db = get_db()
    db.execute("INSERT OR IGNORE INTO highlights (message_id, ts) VALUES (?, ?)", (msg.id, time.time()))
    db.commit()
    logging.info("Highlight: message %s (%d voters)", msg.id, count)
