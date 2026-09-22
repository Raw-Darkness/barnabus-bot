"""Text commands for the owner and moderators, accepted in DMs or the mod channel."""
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import discord

from . import core
from .db import add_user_record, delete_user_data, get_user_records, summarize_user_record_counts
from .moderation import flag_history, is_privileged

_OWNER_COMMANDS = {"!reload"}
_MOD_COMMANDS = {"!summary", "!activity", "!whois", "!flags", "!search", "!note", "!record", "!forget", "!checkperms", "!help"}


def is_command_channel(message: discord.Message) -> bool:
    return isinstance(message.channel, discord.DMChannel) or getattr(message.channel, "id", None) == core.mod_channel_id()


def _tz():
    from .summary import summary_tz
    return summary_tz()


# Which permissions each configured channel needs, so a missing override is
# reported by name instead of surfacing later as a silent failure.
_CHANNEL_NEEDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("ModChannelID", "mod channel", ("view_channel", "send_messages", "read_message_history", "embed_links")),
    ("ModLogChannelID", "mod log", ("view_channel", "send_messages", "embed_links")),
    ("HoneypotChannelID", "honeypot", ("view_channel", "read_message_history", "manage_messages")),
    ("HighlightsChannelID", "highlights", ("view_channel", "send_messages", "embed_links", "attach_files")),
    ("QuestionsForumID", "questions forum", ("view_channel", "send_messages_in_threads", "read_message_history")),
    ("StatsMemberChannelID", "member stats channel", ("view_channel", "manage_channels")),
    ("StatsPlayersChannelID", "players stats channel", ("view_channel", "manage_channels")),
    ("XPAnnounceChannelID", "level-up channel", ("view_channel", "send_messages")),
]
_GUILD_NEEDS = ("ban_members", "kick_members", "manage_roles", "manage_channels", "manage_messages", "view_audit_log")


def permission_report() -> str:
    lines: list[str] = []
    for guild in core.bot.guilds:
        me = guild.me
        if me is None:
            continue
        lines.append(f"**{guild.name}** — role position {me.top_role.position} ({me.top_role.name})")
        missing = [p for p in _GUILD_NEEDS if not getattr(me.guild_permissions, p)]
        lines.append("Server-wide: " + ("✅ all needed permissions" if not missing else "❌ missing " + ", ".join(missing)))
        if not me.guild_permissions.administrator:
            for lvl, rid in (core.config.get("XPRoleRewards") or {}).items():
                role = guild.get_role(int(rid))
                if role is None:
                    lines.append(f"❌ rank role for level {lvl} ({rid}) does not exist")
                elif role >= me.top_role:
                    lines.append(f"❌ rank role **{role.name}** is above my role — I cannot grant it")
        for key, label, needs in _CHANNEL_NEEDS:
            cid = core.cfg_int(key)
            if not cid:
                continue
            ch = guild.get_channel(cid) or guild.get_thread(cid)
            if ch is None:
                lines.append(f"❌ {label} ({cid}): not found or not visible to me")
                continue
            perms = ch.permissions_for(me)
            lacking = [p for p in needs if not getattr(perms, p)]
            lines.append(f"{'✅' if not lacking else '❌'} {label} <#{cid}>" + (f": missing {', '.join(lacking)}" if lacking else ""))
        if not core.bot.intents.message_content:
            lines.append("⚠️ Message Content intent is off: mod log text, spam/flood, safety monitor, "
                         "!search/!whois counts and text commands in channels are inactive until Discord approves it")
    return "\n".join(lines) or "Not in any server."


async def handle_bot_command(message: discord.Message) -> bool:
    if not is_command_channel(message):
        return False
    text = (message.content or "").strip()
    if not text.startswith("!"):
        return False
    parts = text.split(None, 1)
    cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    if cmd not in _OWNER_COMMANDS and cmd not in _MOD_COMMANDS:
        return False
    is_owner = message.author.id == core.owner_id()
    if cmd in _OWNER_COMMANDS and not is_owner:
        await message.channel.send("Owner only.")
        return True
    if not is_owner and not await is_privileged(message.author.id):
        return False  # silently ignore
    ch = message.channel

    if cmd == "!summary":
        from .summary import generate_server_summary
        m = re.match(r"(\d+)\s*h?", arg)
        hours = int(m.group(1)) if m else 24
        await ch.send(f"⏳ Reading the last {hours}h. This takes a minute.")
        digest = await generate_server_summary(hours=hours)
        for chunk in core.chunks(digest or "No significant activity found."):
            await ch.send(chunk)
        return True

    if cmd == "!reload":
        try:
            core.load_config()
            from .lore import load_lore
            load_lore()
            await ch.send("✅ Config reloaded.")
        except Exception as e:
            await ch.send(f"❌ Reload failed: {e}")
        return True

    if cmd == "!activity":
        await ch.send("⏳ Scanning channels…")
        after = datetime.now(timezone.utc) - timedelta(hours=6)
        counts: list[tuple[str, int]] = []
        for guild in core.bot.guilds:
            for tc in guild.text_channels:
                try:
                    if guild.me and not tc.permissions_for(guild.me).read_message_history:
                        continue
                    n = 0
                    async for _ in tc.history(after=after, limit=200):
                        n += 1
                    if n:
                        counts.append((f"#{tc.name}", n))
                except Exception:
                    continue
        counts.sort(key=lambda x: -x[1])
        await ch.send("**Activity (last 6h):**\n" + "\n".join(f"{n}: {c} msgs" for n, c in counts[:10])
                      if counts else "No activity in the last 6 hours.")
        return True

    if cmd == "!whois":
        if not arg:
            await ch.send("Usage: `!whois <username or user_id>`")
            return True
        await ch.send("⏳ Looking up…")
        found: discord.Member | None = None
        for guild in core.bot.guilds:
            try:
                uid = int(arg)
                found = guild.get_member(uid) or await guild.fetch_member(uid)
            except (ValueError, discord.NotFound):
                pass
            if not found:
                q = arg.lower()
                found = next((m for m in guild.members if q in m.display_name.lower() or q in m.name.lower()), None)
            if found:
                break
        if not found:
            await ch.send(f"No member matching `{arg}`.")
            return True
        m = found
        now = datetime.now(timezone.utc)
        roles = ", ".join(r.name for r in m.roles if r.name != "@everyone") or "None"
        msg_count = 0
        after = now - timedelta(hours=24)
        for tc in m.guild.text_channels:
            try:
                if m.guild.me and not tc.permissions_for(m.guild.me).read_message_history:
                    continue
                async for msg in tc.history(after=after, limit=100):
                    if msg.author.id == m.id:
                        msg_count += 1
            except Exception:
                continue
        info = (f"**{m.display_name}** ({m.name}, ID: {m.id})\n"
                f"Account created: {m.created_at:%Y-%m-%d} ({(now - m.created_at).days}d ago)\n")
        if m.joined_at:
            info += f"Joined server: {m.joined_at:%Y-%m-%d} ({(now - m.joined_at).days}d ago)\n"
        info += f"Roles: {roles}\nMessages (last 24h): ~{msg_count}"
        rec = summarize_user_record_counts(m.id)
        if rec:
            info += f"\nRecord: {rec} — `!record {m.id}` for details"
        await ch.send(info)
        return True

    if cmd == "!flags":
        cutoff = time.time() - 86400
        recent = [f for f in flag_history if f[0] >= cutoff]
        if not recent:
            await ch.send("No flags in the last 24 hours.")
            return True
        tz = _tz()
        lines = []
        for ts, title, details in reversed(recent):
            short = details.replace("\n", " | ")
            lines.append(f"`{datetime.fromtimestamp(ts, tz=tz):%H:%M}` **{title}** — {short[:200] + ('…' if len(short) > 200 else '')}")
        for chunk in core.chunks(f"**Flags (last 24h): {len(recent)}**\n" + "\n".join(lines)):
            await ch.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        return True

    if cmd == "!search":
        if len(arg) < 3:
            await ch.send("Usage: `!search <term>` (at least 3 characters)")
            return True
        await ch.send(f"⏳ Searching for `{arg}`…")
        q = arg.lower()
        after = datetime.now(timezone.utc) - timedelta(hours=24)
        results: list[tuple[str, str, str, datetime]] = []
        for guild in core.bot.guilds:
            for tc in guild.text_channels:
                try:
                    if guild.me and not tc.permissions_for(guild.me).read_message_history:
                        continue
                    async for msg in tc.history(after=after, limit=200):
                        if msg.author.bot:
                            continue
                        c = msg.content or ""
                        if q in c.lower():
                            results.append((f"#{tc.name}", msg.author.display_name, c[:150] + ("…" if len(c) > 150 else ""), msg.created_at))
                except Exception:
                    continue
        if not results:
            await ch.send(f"No results for `{arg}` in the last 24h.")
            return True
        results.sort(key=lambda x: x[3], reverse=True)
        tz = _tz()
        body = "\n".join(f"`{ts.astimezone(tz):%H:%M}` {c} — **{a}**: {s}" for c, a, s, ts in results[:20])
        for chunk in core.chunks(f"**Search results for `{arg}` (last 24h): {min(len(results), 20)}**\n" + body):
            await ch.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        return True

    if cmd == "!note":
        np = arg.split(None, 1)
        target = core.parse_user_ref(np[0]) if np else None
        if not target or len(np) < 2:
            await ch.send("Usage: `!note <user_id or @mention> <text>`")
            return True
        add_user_record(target, "note", np[1], moderator_id=message.author.id)
        await ch.send(f"📝 Note added for <@{target}> (`{target}`).", allowed_mentions=discord.AllowedMentions.none())
        return True

    if cmd == "!record":
        target = core.parse_user_ref(arg)
        if not target:
            await ch.send("Usage: `!record <user_id or @mention>`")
            return True
        rows = get_user_records(target, limit=25)
        if not rows:
            await ch.send(f"No records for <@{target}> (`{target}`).", allowed_mentions=discord.AllowedMentions.none())
            return True
        lines = [f"**Record for <@{target}>** (`{target}`) — {summarize_user_record_counts(target)}"]
        for kind, detail, mod_id, ts in rows:
            entry = f"<t:{int(ts)}:d> · **{kind.replace('_', ' ')}**"
            if detail:
                entry += f" — {detail[:150]}"
            if mod_id:
                entry += f" (by <@{mod_id}>)"
            lines.append(entry)
        for chunk in core.chunks("\n".join(lines)):
            await ch.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        return True

    if cmd == "!forget":
        target = core.parse_user_ref(arg)
        if not target:
            await ch.send("Usage: `!forget <user_id or @mention>` — erases the member's records, notes and XP (data deletion request).")
            return True
        r, x = delete_user_data(target)
        logging.info("Data deletion for %s by %s: %d records, %d xp rows", target, message.author, r, x)
        await ch.send(f"🧹 Erased {r} record(s) and {x} XP row(s) for `{target}`.")
        return True

    if cmd == "!checkperms":
        report = permission_report()
        for chunk in core.chunks(report):
            await ch.send(chunk)
        return True

    if cmd == "!help":
        help_text = (
            "**Commands** (DM or mod channel):\n"
            "`!summary [Nh]` — server digest (default 24h)\n"
            "`!activity` — most active channels (last 6h)\n"
            "`!whois <user>` — look up a member\n"
            "`!flags` — recent alerts (last 24h)\n"
            "`!search <term>` — search messages (last 24h)\n"
            "`!note <user> <text>` — add a note to a user's record\n"
            "`!record <user>` — show a user's record (flags, honeypot trips, notes)\n"
            "`!forget <user>` — erase a member's records, notes and XP (deletion request)\n"
            "`!checkperms` — verify the bot's permissions in every configured channel\n"
            "`!help` — this message\n"
            "Public: `!rank [user]`, `!top`; slash: `/ask`, `/wiki`, `/lore`; right-click → Apps → Translate"
        )
        if is_owner:
            help_text += "\n\n**Owner only:**\n`!reload` — reload config and lore from disk"
        await ch.send(help_text)
        return True
    return False
