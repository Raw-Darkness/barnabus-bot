"""Event wiring and process lifecycle."""
import asyncio
import functools
import logging
import os
import signal
import sys

import aiohttp
import discord

from . import core, db, lore, faq, stats, summary
from . import wiki, translate, highlights  # noqa: F401  (register commands/events)
from .commands import handle_bot_command, permission_report
from .moderation import check_flood, check_spam, honeypot_guard
from .safety import monitor_message
from .xp import award_xp, handle_xp_command

_started = False
_mention_buckets: dict[int, core.TokenBucket] = {}


@core.bot.event
async def on_ready():
    global _started
    logging.info("READY as %s (id=%s) pid=%s", core.bot.user, getattr(core.bot.user, "id", "?"), os.getpid())
    if not _started:
        _started = True
        try:
            logging.info("Permission check:\n%s", permission_report().replace("**", ""))
        except Exception:
            logging.exception("Permission check failed")
        for coro in (_config_watch(), _sync_commands(), _faq_loop(), stats.loop(), summary.scheduler(), _expire_loop(), _heartbeat()):
            core.bot.loop.create_task(coro)


@core.bot.event
async def on_message(message: discord.Message):
    if core.bot.user is None or message.author.id == core.bot.user.id:
        return
    # Honeypot first: the trap must fire before anything else looks at the message.
    try:
        if await honeypot_guard(message):
            return
    except Exception:
        logging.exception("honeypot_guard error")
    # Passive safety monitor: members, and listed bots (e.g. the chat bot's replies).
    try:
        await monitor_message(message)
    except Exception:
        logging.exception("safety monitor error")
    if message.author.bot:
        return
    if message.guild:
        try:
            await check_spam(message)
            await check_flood(message)
        except Exception:
            logging.exception("spam/flood check error")
    if core.is_ignored(message):
        return
    try:
        if await handle_bot_command(message):
            return
    except Exception:
        logging.exception("bot command error")
    try:
        if await handle_xp_command(message):
            return
    except Exception:
        logging.exception("xp command error")
    if message.guild and not (message.content or "").startswith("!"):
        asyncio.create_task(award_xp(message))
    # Barnabus does not chat. Anyone who addresses him directly gets pointed at the tools.
    addressed = isinstance(message.channel, discord.DMChannel) or (message.guild and core.bot.user in message.mentions)
    if addressed and core.bucket(_mention_buckets, message.author.id, 1, 1.0 / 120.0).consume():
        await core.safe_send(message.channel, core.text(
            "MentionReply",
            "I don't do conversation. Use `/ask` for the game, `/lore` for the world, `/wiki` for the pages. "
            "Now get out of my light."))


async def _sync_commands():
    """Register slash commands. Guild-scoped sync is instant; global takes up to an hour."""
    await core.bot.wait_until_ready()
    try:
        gid = core.cfg_int("AppCommandGuildID")
        if gid:
            guild = discord.Object(id=gid)
            core.tree.copy_global_to(guild=guild)
            synced = await core.tree.sync(guild=guild)
        else:
            synced = await core.tree.sync()
        logging.info("Synced %d app command(s): %s", len(synced), [c.name for c in synced])
    except Exception:
        logging.exception("App command sync failed")


async def _faq_loop():
    await core.bot.wait_until_ready()
    while not core.bot.is_closed():
        try:
            await faq.scan_once()
        except Exception:
            logging.exception("FAQ scan failed")
        await asyncio.sleep(600)


async def _heartbeat():
    """Ping an external monitor (e.g. healthchecks.io) while connected. When the
    pings stop — process dead, machine down, network gone — the monitor alerts."""
    await core.bot.wait_until_ready()
    while not core.bot.is_closed():
        url = str(core.config.get("HeartbeatURL") or "")
        if url and core.bot.is_ready():
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                    await s.get(url)
            except Exception as e:
                logging.warning("Heartbeat ping failed: %s", e)
        await asyncio.sleep(max(30, core.cfg_int("HeartbeatIntervalSec", 120)))


async def _expire_loop():
    """Blank stored message excerpts past their retention, once an hour."""
    await core.bot.wait_until_ready()
    while not core.bot.is_closed():
        try:
            db.expire_excerpts()
        except Exception:
            logging.exception("Excerpt expiry failed")
        await asyncio.sleep(3600)


async def _config_watch():
    """Reload the config or lore file when it changes on disk."""
    while True:
        await asyncio.sleep(10)
        try:
            if os.path.getmtime(core.CONFIG_PATH) > core.config_mtime:
                core.load_config()
                lore.load_lore()
                logging.info("Config hot-reloaded")
            elif lore.lore_changed():
                lore.load_lore()
                logging.info("Lore hot-reloaded")
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception("Config watch error")


def _signals():
    loop = asyncio.get_event_loop()

    def _shutdown(sig):
        logging.info("Received %s — closing.", sig.name)
        loop.create_task(core.bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, functools.partial(_shutdown, sig))
        except NotImplementedError:
            pass


def main() -> None:
    lore.load_lore()
    _signals()
    token = core.config.get("DiscordToken") or ""
    if not token:
        logging.error("No DiscordToken in %s.", core.CONFIG_PATH)
        sys.exit(78)
    # A rejected token stays rejected until a human fixes it. Exit 78 tells the
    # service unit not to restart, so we never hammer Discord's login endpoint.
    try:
        core.bot.run(token)
    except discord.LoginFailure:
        logging.error("Discord rejected the token. Reset it in the Developer Portal, put it in %s, restart. Not retrying.",
                      core.CONFIG_PATH)
        sys.exit(78)
    except discord.PrivilegedIntentsRequired:
        logging.error("A privileged intent is enabled in %s but not approved in the Developer Portal "
                      "(Bot tab). Either enable it there or set EnableMessageContentIntent / EnableMembersIntent "
                      "to false. Not retrying.", core.CONFIG_PATH)
        sys.exit(78)
