"""MEE6-style XP and rank roles. Level N -> N+1 costs 5N² + 50N + 100 XP."""
import logging
import random
import time

import discord

from . import core
from .db import get_db


def xp_needed_for(level: int) -> int:
    return 5 * level * level + 50 * level + 100


def level_progress(total_xp: int) -> tuple[int, int, int]:
    """(level, xp_into_level, xp_needed_for_next)"""
    level, remaining = 0, int(total_xp)
    while remaining >= xp_needed_for(level):
        remaining -= xp_needed_for(level)
        level += 1
    return level, remaining, xp_needed_for(level)


_DEFAULT_LEVELUP = ["**{name}** — level **{level}**. Steel holds. Keep it that way."]


async def award_xp(message: discord.Message) -> None:
    try:
        if not core.config.get("XPEnabled", True) or message.channel.id in core.cfg_ids("XPExcludedChannels"):
            return
        now = time.time()
        db = get_db()
        row = db.execute("SELECT xp, level, last_award FROM user_xp WHERE user_id = ?", (message.author.id,)).fetchone()
        if row and now - row[2] < int(core.config.get("XPCooldownSec", 60)):
            return
        gain = random.randint(int(core.config.get("XPPerMessageMin", 15)), int(core.config.get("XPPerMessageMax", 25)))
        old_xp, old_level = (row[0], row[1]) if row else (0, 0)
        new_xp = old_xp + gain
        new_level, _, _ = level_progress(new_xp)
        db.execute(
            """INSERT INTO user_xp (user_id, name, xp, level, messages, last_award) VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, xp = excluded.xp, level = excluded.level,
               messages = user_xp.messages + 1, last_award = excluded.last_award""",
            (message.author.id, message.author.display_name, new_xp, new_level, now),
        )
        db.commit()
        if new_level > old_level:
            await _level_up(message, old_level, new_level)
    except Exception:
        logging.exception("award_xp failed")


async def _level_up(message: discord.Message, old_level: int, new_level: int) -> None:
    rewards = {int(k): int(v) for k, v in (core.config.get("XPRoleRewards") or {}).items()}
    crossed = [lvl for lvl in sorted(rewards) if old_level < lvl <= new_level]
    tier = crossed[-1] if crossed else None
    if tier is None and core.config.get("XPAnnounceRanksOnly"):
        return
    template = (core.config.get("XPTierMessages") or {}).get(str(tier)) if tier is not None else None
    if not template:
        template = str(random.choice(core.config.get("LevelUpMessages") or _DEFAULT_LEVELUP))
    try:
        text = template.format(name=message.author.display_name, level=new_level)
    except Exception:
        text = f"{message.author.display_name} reached level {new_level}."
    channel = await core.get_channel(core.cfg_int("XPAnnounceChannelID")) or message.channel
    await core.safe_send(channel, text)

    # Promotion is a SWAP: grant the highest earned rank, drop lower ranks, never
    # touch tiers above the earned one (protects manually granted top roles).
    member = message.author
    if message.guild and isinstance(member, discord.Member) and rewards:
        earned = [lvl for lvl in sorted(rewards) if lvl <= new_level]
        if not earned:
            return
        try:
            held = {r.id for r in member.roles}
            higher_held = [lvl for lvl in rewards if lvl > earned[-1] and rewards[lvl] in held]
            top = max(higher_held) if higher_held else earned[-1]
            if not higher_held:
                role = message.guild.get_role(rewards[earned[-1]])
                if role and role not in member.roles:
                    await member.add_roles(role, reason=f"Level {earned[-1]} rank")
            lower = {rewards[lvl] for lvl in rewards if lvl < top}
            drop = [r for r in member.roles if r.id in lower]
            if drop:
                await member.remove_roles(*drop, reason="Rank promotion")
        except Exception:
            logging.exception("Failed to update rank roles")


async def handle_xp_command(message: discord.Message) -> bool:
    """Public: !rank / !level [user], !top / !leaderboard."""
    parts = (message.content or "").strip().split(None, 1)
    if not parts:
        return False
    cmd = parts[0].lower()
    if cmd not in ("!rank", "!level", "!top", "!leaderboard") or not core.config.get("XPEnabled", True):
        return False
    arg = parts[1].strip() if len(parts) > 1 else ""
    db = get_db()
    if cmd in ("!rank", "!level"):
        target = (core.parse_user_ref(arg) if arg else None) or message.author.id
        row = db.execute("SELECT name, xp, messages FROM user_xp WHERE user_id = ?", (target,)).fetchone()
        if not row:
            who = "You haven't" if target == message.author.id else "They haven't"
            await core.safe_send(message.channel, f"{who} earned any XP yet. Speak up; the forge counts every word.")
            return True
        name, xp, msgs = row
        level, progress, needed = level_progress(xp)
        rank = db.execute("SELECT COUNT(*) + 1 FROM user_xp WHERE xp > ?", (xp,)).fetchone()[0]
        await core.safe_send(message.channel,
                             f"**{name}** — Level **{level}** · Rank **#{rank}**\n"
                             f"XP: {xp} ({progress}/{needed} into the next level) · Messages counted: {msgs}")
        return True
    rows = db.execute("SELECT name, level, xp FROM user_xp ORDER BY xp DESC LIMIT 10").fetchall()
    if not rows:
        await core.safe_send(message.channel, "The board is empty. Nobody has said anything worth counting.")
        return True
    medals = ["🥇", "🥈", "🥉"]
    lines = ["**🏆 Leaderboard**"] + [
        f"{medals[i] if i < 3 else f'`#{i + 1}`'} **{n}** — Level {lv} · {x:,} XP" for i, (n, lv, x) in enumerate(rows)]
    await core.safe_send(message.channel, "\n".join(lines))
    return True
