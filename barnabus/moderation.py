"""Mod alerts, spam/flood detection, the honeypot channel and the mod log."""
import asyncio
import logging
import random
import re
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import discord

from . import core
from .db import add_user_record

# ---- Mod alerts --------------------------------------------------------------
# Recent flags for !flags: (timestamp, title, details)
flag_history: deque[tuple[float, str, str]] = deque(maxlen=200)


async def flag_to_mods(title: str, details: str, ping: bool = True) -> None:
    """Post to the mod channel. ping=False posts silently, for contextual matches
    that may be false positives — a "she is 5 foot 6" must never page every mod."""
    flag_history.append((time.time(), title, details))
    ch = await core.get_channel(core.mod_channel_id())
    if ch is None:
        return
    prefix = "@here " if ping and core.config.get("ModAlertPing", True) else ""
    await core.safe_send(ch, f"{prefix}⚠️ **{title}**\n{details}",
                         allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False))


async def is_privileged(user_id: int) -> bool:
    """Owner, or holder of the moderator role in any guild."""
    if user_id == core.owner_id():
        return True
    role_id = core.mod_role_id()
    for guild in core.bot.guilds:
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            if member and any(r.id == role_id for r in member.roles):
                return True
        except Exception:
            continue
    return False


# ---- Spam & flood -----------------------------------------------------------
_INVITE_RE = re.compile(r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)/\S+", re.IGNORECASE)
_SCAM_URL_RE = re.compile(r"(free\s*nitro|steam\s*community\.ru|discord.*gift|claim.*reward|verify.*airdrop)", re.IGNORECASE)
_recent_messages: dict[int, deque[tuple[int, str, float]]] = {}
_FLOOD_WINDOW = 30.0
_FLOOD_CHANNEL_THRESHOLD = 3


async def check_spam(message: discord.Message) -> bool:
    """Flag invite/scam links, especially from new accounts."""
    if message.guild is None:
        return False
    text = message.content or ""
    has_invite, has_scam = bool(_INVITE_RE.search(text)), bool(_SCAM_URL_RE.search(text))
    if not (has_invite or has_scam):
        return False
    member = message.author if isinstance(message.author, discord.Member) else None
    if member is None:
        return False
    age = datetime.now(timezone.utc) - member.created_at
    is_new = age < timedelta(hours=int(core.config.get("SpamNewAccountHours", 24)))
    if not (is_new or has_scam):
        return False
    reason = []
    if is_new:
        reason.append(f"new account ({age.total_seconds() / 3600:.1f}h old)")
    if has_invite:
        reason.append("Discord invite link")
    if has_scam:
        reason.append("scam URL pattern")
    await flag_to_mods(
        "Possible spam",
        f"User: **{member.display_name}** ({member.id})\nChannel: <#{message.channel.id}>\n"
        f"Reason: {', '.join(reason)}\nMessage: {text[:200]}",
    )
    add_user_record(member.id, "spam_flag", f"{', '.join(reason)} | {text[:150]}")
    return True


async def check_flood(message: discord.Message) -> bool:
    """Same user posting the same message across several channels in a short window."""
    if message.guild is None:
        return False
    content = (message.content or "").strip().lower()
    if len(content) < 10:
        return False
    now = time.time()
    hist = _recent_messages.setdefault(message.author.id, deque(maxlen=50))
    hist.append((message.channel.id, content, now))
    same = {c for c, m, ts in hist if now - ts <= _FLOOD_WINDOW and m == content}
    if len(same) < _FLOOD_CHANNEL_THRESHOLD:
        return False
    member = message.author
    await flag_to_mods(
        "Flood detected",
        f"User: **{member.display_name}** ({member.id})\n"
        f"Same message posted in {len(same)} channels within {int(_FLOOD_WINDOW)}s\nContent: {content[:200]}",
    )
    add_user_record(member.id, "flood_flag", f"same msg in {len(same)} channels | {content[:150]}")
    hist.clear()
    return True


# ---- Honeypot ----------------------------------------------------------------
# Users the bot itself just removed, so the mod log skips the ban/unban/delete
# events that action generates — one honeypot trip, one log entry.
_bot_actioned: dict[int, float] = {}


def recently_actioned(user_id: int | None = None, window: float = 300.0) -> bool:
    now = time.time()
    for uid, ts in list(_bot_actioned.items()):
        if now - ts > window:
            del _bot_actioned[uid]
    return bool(_bot_actioned) if user_id is None else user_id in _bot_actioned


_DEFAULT_QUIPS = [
    "{name} walked into the one room with a sign on the door. Brittle. Out they go.",
    "The trap was not subtle. {name} sprang it anyway. Over the side.",
    "{name} took on water in the honeypot. I don't bail for anyone.",
    "Scrap metal. {name} has been thrown back in the sea.",
]


def pick_quip(name: str) -> str:
    quips = core.config.get("HoneypotQuips") or _DEFAULT_QUIPS
    try:
        return str(random.choice(quips)).format(name=name)
    except Exception:
        return f"{name} tripped the honeypot and was removed."


async def honeypot_guard(message: discord.Message) -> bool:
    """Anyone who posts in the honeypot channel is removed and their recent messages wiped."""
    try:
        trap = core.trap_channel_id()
        if message.guild is None or message.author.bot or not trap or message.channel.id != trap:
            return False
        guild = message.guild
        member = message.author if isinstance(message.author, discord.Member) else None
        if member is None:
            try:
                member = await guild.fetch_member(message.author.id)
            except Exception:
                logging.exception("Honeypot: cannot fetch member")
                return False
        if member == guild.owner or member.guild_permissions.administrator:
            return False
        if any(r.id in core.exempt_role_ids() for r in member.roles):
            return False

        try:
            await message.delete()
        except Exception:
            logging.exception("Honeypot: could not delete trigger message")

        reason = f"Posted in honeypot channel ({trap})"
        action = str(core.config.get("HoneypotAction", "kick")).lower()
        delete_seconds = max(0, min(604800, int(core.config.get("HoneypotDeleteSeconds", 600))))
        removal_word = "banned" if action == "ban" else "kicked"
        removed_ok = False
        total_deleted = -1  # -1 = Discord wiped messages server-side

        # Ban with delete_message_seconds so Discord wipes recent messages server-side,
        # then lift the ban for "kick" semantics (softban): the user may rejoin.
        _bot_actioned[member.id] = time.time()
        try:
            await guild.ban(member, reason=reason, delete_message_seconds=delete_seconds)
            if action != "ban":
                await guild.unban(member, reason="Honeypot softban — kick semantics")
            removed_ok = True
        except discord.Forbidden:
            logging.warning("Honeypot: no ban permission; falling back to kick + manual purge")
        except Exception:
            logging.exception("Honeypot: ban failed; falling back to kick + manual purge")

        if not removed_ok:
            try:
                await guild.kick(member, reason=reason)
                removed_ok, removal_word = True, "kicked"
            except Exception:
                logging.exception("Honeypot: kick failed")
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=delete_seconds)
            total_deleted = 0

            async def purge(ch: discord.TextChannel) -> int:
                try:
                    me = guild.me or await guild.fetch_member(core.bot.user.id)
                    p = ch.permissions_for(me)
                    if not (p.read_message_history and p.manage_messages):
                        return 0
                    return len(await ch.purge(limit=None, after=cutoff, check=lambda m: m.author.id == member.id, bulk=True))
                except Exception:
                    return 0

            for sweep in range(2):
                if sweep:
                    await asyncio.sleep(3)
                for ch in guild.text_channels:
                    total_deleted += await purge(ch)

        logging.info("Honeypot: %s %s (%s)", removal_word if removed_ok else "FAILED to remove", member,
                     "server-side wipe" if total_deleted < 0 else f"purged ~{total_deleted}")
        add_user_record(member.id, "honeypot_ban" if action == "ban" else "honeypot_kick",
                        f"posted in honeypot channel; removed_ok={removed_ok}")

        notify = await core.get_channel(core.cfg_int("HoneypotNotifyChannelID") or core.modlog_channel_id() or core.mod_channel_id())
        if notify:
            status = removal_word if removed_ok else "NOT removed (action failed)"
            if total_deleted < 0:
                cleanup = f"🧹 Discord wiped their messages from the last {delete_seconds // 60} min."
            else:
                cleanup = f"🧹 Deleted ~{total_deleted} message(s) from the last {delete_seconds // 60} min."
            embed = discord.Embed(title=f"🔨 Honeypot: {member.display_name} {status}",
                                  description=f"{cleanup}\n{pick_quip(member.display_name)}",
                                  color=0x992D22 if removed_ok else 0xE67E22)
            embed.add_field(name="User", value=f"{member} ({member.id})")
            try:
                await notify.send(embed=embed)
            except Exception:
                logging.exception("Honeypot: could not notify mods")
        return True
    except Exception:
        logging.exception("Honeypot guard failed")
        return False


# ---- Mod log -----------------------------------------------------------------
async def modlog_send(embed: discord.Embed) -> None:
    ch = await core.get_channel(core.modlog_channel_id())
    if ch is None:
        return
    try:
        await ch.send(embed=embed)
    except Exception:
        logging.exception("Mod log send failed")


def _modlog_skip_channel(channel_id: int) -> bool:
    return channel_id == core.modlog_channel_id() or channel_id in core.cfg_ids("ModLogIgnoredChannels")


@core.bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    try:
        if payload.guild_id is None or _modlog_skip_channel(payload.channel_id) or not core.config.get("ModLogDeletes", True):
            return
        msg = payload.cached_message
        if msg is not None and msg.author.bot:
            return
        if payload.channel_id == core.trap_channel_id():
            return  # covered by the honeypot entry
        if msg is not None and recently_actioned(msg.author.id):
            return  # wiped by our own ban
        embed = discord.Embed(title="Message deleted", color=0xE74C3C)
        if msg is not None:
            embed.add_field(name="Author", value=f"{msg.author} ({msg.author.id})", inline=False)
            if msg.content:
                embed.add_field(name="Content", value=core.trunc(msg.content), inline=False)
            if msg.attachments:
                embed.add_field(name="Attachments", value=core.trunc(", ".join(a.filename for a in msg.attachments), 200), inline=False)
        else:
            embed.description = "Content unknown (message was not cached)."
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
        embed.add_field(name="When", value=f"<t:{int(time.time())}:R>")
        await modlog_send(embed)
    except Exception:
        logging.exception("on_raw_message_delete failed")


@core.bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    try:
        if payload.guild_id is None or _modlog_skip_channel(payload.channel_id) or recently_actioned():
            return
        await modlog_send(discord.Embed(
            title="Bulk delete",
            description=f"{len(payload.message_ids)} messages removed in <#{payload.channel_id}> (e.g. a purge).",
            color=0xE74C3C))
    except Exception:
        logging.exception("on_raw_bulk_message_delete failed")


@core.bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    try:
        if payload.guild_id is None or _modlog_skip_channel(payload.channel_id) or not core.config.get("ModLogEdits", True):
            return
        data = payload.data or {}
        if "content" not in data:
            return  # embed/pin/component update
        new = data.get("content") or ""
        cached = payload.cached_message
        if cached is not None:
            if cached.author.bot or (cached.content or "") == new:
                return
            old, author_desc = cached.content or "", f"{cached.author} ({cached.author.id})"
            # Trivial edits (typo fixes) are noise in a busy server.
            min_delta = int(core.config.get("ModLogEditMinChange", 0))
            if min_delta and abs(len(new) - len(old)) < min_delta and old[:len(old) // 2] == new[:len(old) // 2]:
                return
        else:
            author = data.get("author") or {}
            if author.get("bot"):
                return
            old, author_desc = "*unknown (not cached)*", f"<@{author.get('id', '?')}> ({author.get('id', '?')})"
        embed = discord.Embed(title="Message edited", color=0xE67E22)
        embed.add_field(name="Author", value=author_desc, inline=False)
        embed.add_field(name="Before", value=core.trunc(old), inline=False)
        embed.add_field(name="After", value=core.trunc(new), inline=False)
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        embed.add_field(name="Where", value=f"<#{payload.channel_id}> · [jump]({jump})")
        await modlog_send(embed)
    except Exception:
        logging.exception("on_raw_message_edit failed")


@core.bot.event
async def on_member_ban(guild: discord.Guild, user):
    try:
        if recently_actioned(user.id):
            return
        embed = discord.Embed(title="Member banned", color=0x992D22)
        embed.add_field(name="User", value=f"{user} ({user.id})")
        await modlog_send(embed)
    except Exception:
        logging.exception("on_member_ban failed")


@core.bot.event
async def on_member_unban(guild: discord.Guild, user):
    try:
        if recently_actioned(user.id):
            return
        embed = discord.Embed(title="Member unbanned", color=0x2ECC71)
        embed.add_field(name="User", value=f"{user} ({user.id})")
        await modlog_send(embed)
    except Exception:
        logging.exception("on_member_unban failed")
