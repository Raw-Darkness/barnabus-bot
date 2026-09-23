"""Shared state: logging, hot-reloadable config, the Discord client and small helpers.

Every module does `from . import core` and reads `core.config` / `core.bot` at call
time. The config dict is updated in place on reload, so a reference never goes stale.
"""
import json
import logging
import os
import re
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import discord
from discord import app_commands

# ---- Logging ---------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
_file_handler = TimedRotatingFileHandler("app.log", when="midnight", interval=1, backupCount=7)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
if not any(isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger().handlers):
    logging.getLogger().addHandler(_file_handler)

# ---- Config ----------------------------------------------------------------
_SECRET_KEYS = {"DiscordToken", "OpenAPIKey", "Personality"}


def _default_config_path() -> str:
    for cand in ("Config.json", "Barnabus.json"):
        if os.path.exists(cand):
            return cand
    return "Config.json"


CONFIG_PATH = os.environ.get("BOT_CONFIG") or _default_config_path()
config: dict[str, Any] = {}
config_mtime: float = 0.0


def load_config() -> None:
    """(Re)load the config file into the shared dict, in place."""
    global config_mtime
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Secrets may come from the environment instead of the file, which is how
    # container platforms (Fly, Railway, Render) hand them over.
    for key, env in (("DiscordToken", "DISCORD_TOKEN"), ("OpenAPIKey", "OPENROUTER_API_KEY")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    config.clear()
    config.update(data)
    config_mtime = os.path.getmtime(CONFIG_PATH)
    logging.info("Loaded config %s: %s", CONFIG_PATH, {k: v for k, v in config.items() if k not in _SECRET_KEYS})


load_config()


def cfg_int(key: str, default: int = 0) -> int:
    try:
        return int(config.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def cfg_ids(key: str) -> set[int]:
    out: set[int] = set()
    for v in config.get(key) or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            pass
    return out


def text(key: str, default: str) -> str:
    """A user-facing string, overridable from the config so the voice can be tuned without code."""
    val = config.get(key)
    return str(val) if val else default


def bot_name() -> str:
    return str(config.get("Name") or "Barnabus")


# Derived IDs are read through functions so a hot reload takes effect immediately.
def owner_id() -> int: return cfg_int("OwnerID") or cfg_int("SummaryOwnerID")
def mod_channel_id() -> int: return cfg_int("ModChannelID")
def mod_role_id() -> int: return cfg_int("ModRoleID")
def modlog_channel_id() -> int: return cfg_int("ModLogChannelID")
def trap_channel_id() -> int: return cfg_int("HoneypotChannelID")
def exempt_role_ids() -> set[int]: return cfg_ids("HoneypotExemptRoleIDs")
def ignored_users() -> set[int]: return cfg_ids("IgnoredUsers")


# ---- Discord client --------------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
# Message Content is privileged: Discord must approve it for this application.
# Until then the bot can run with EnableMessageContentIntent false — honeypot,
# XP, highlights, stats, mod-log ban events, slash commands and DM commands all
# work without it; anything that reads guild message text does not.
intents.message_content = bool(config.get("EnableMessageContentIntent", True))
# !whois by name and accurate member counts want the privileged Server Members
# intent — enable it in the developer portal FIRST, then set EnableMembersIntent.
if config.get("EnableMembersIntent"):
    intents.members = True
# Large cache so deleted/edited message content is usually available for the mod log.
bot = discord.Client(intents=intents, max_messages=10000)
tree = app_commands.CommandTree(bot)


# ---- Helpers ---------------------------------------------------------------
def clamp_2000(s: str) -> str:
    return (s or "")[:2000]


def chunks(s: str, n: int = 1900) -> list[str]:
    s = s or ""
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


def trunc(s: str, n: int = 900) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


async def safe_send(channel: discord.abc.Messageable, content: str | None = None, **kwargs):
    try:
        if content is not None:
            return await channel.send(clamp_2000(content), **kwargs)
        return await channel.send(**kwargs)
    except Exception:
        logging.exception("safe_send failed")


async def get_channel(channel_id: int):
    if not channel_id:
        return None
    try:
        return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    except Exception:
        logging.exception("Cannot resolve channel %s", channel_id)
        return None


def channel_key(message: discord.Message) -> int:
    """Threads count as their parent channel."""
    parent = getattr(message.channel, "parent", None)
    return parent.id if parent is not None else message.channel.id


def parse_user_ref(arg: str) -> int | None:
    """Accept a raw user ID or a <@mention>."""
    a = (arg or "").strip()
    m = re.match(r"^<@!?(\d+)>$", a) or re.match(r"^(\d+)$", a)
    return int(m.group(1)) if m else None


def is_ignored(message: discord.Message) -> bool:
    if message.author.id in ignored_users():
        return True
    low = (message.content or "").lower()
    return any(w.lower() in low for w in config.get("IgnoredWords") or [])


class TokenBucket:
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = time.time()
        self.refill_rate = refill_rate

    def consume(self, n: int = 1) -> bool:
        now = time.time()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill_rate)
        self.last = now
        if n <= self.tokens:
            self.tokens -= n
            return True
        return False


def bucket(store: dict[int, TokenBucket], key: int, capacity: int, refill_rate: float) -> TokenBucket:
    b = store.get(key)
    if b is None:
        b = store[key] = TokenBucket(capacity, refill_rate)
    return b
