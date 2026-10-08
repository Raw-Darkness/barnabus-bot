"""Private file bridge. Discord data is input, never instructions or executable code.

Only the running bot talks to Discord. Owner-operated SSH transfers snapshots and
explicit Markdown submissions; no Unreal files or model calls live on the VPS.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import discord

from . import core

MAX_MARKDOWN = 64 * 1024
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


def settings():
    guild = core.cfg_int("BugReviewGuildID")
    internal = core.cfg_ids("BugReviewInternalForumIDs")
    public = core.cfg_ids("BugReviewPublicForumIDs")
    if not guild or not internal or internal & public or any(i <= 0 for i in internal | public):
        raise ValueError("Configure a guild and disjoint internal/public forum lists")
    return guild, internal, public


def root():
    path = Path(core.config.get("BugReviewDirectory") or "bug-review")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("outbox", "receipts"):
        (path / name).mkdir(exist_ok=True, mode=0o700)
    return path


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.chmod(0o600)
    temp.replace(path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def content_available():
    # The client's intents are fixed until restart; hot-reloading config alone
    # must never advertise that message content has become available.
    return bool(core.config.get("EnableMessageContentIntent", True) is True and core.bot.intents.message_content)


def stable_evidence(value):
    """CDN signature refreshes are not changes to the underlying report."""
    if isinstance(value, dict):
        return {k: stable_evidence(v) for k, v in value.items()}
    if isinstance(value, list):
        return [stable_evidence(v) for v in value]
    if isinstance(value, str) and value.startswith("https://"):
        try:
            parts = urlsplit(value)
            if parts.hostname in {"cdn.discordapp.com", "media.discordapp.net"}:
                query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k not in {"ex", "is", "hm"}])
                return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
        except ValueError:
            pass
    return value


def revision(report):
    payload = {k: report[k] for k in ("id", "guild_id", "forum_id", "title", "messages", "history_truncated")}
    return hashlib.sha256(json.dumps(stable_evidence(payload), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def resolve_forum(forum_id, guild_id):
    forum = await core.bot.fetch_channel(forum_id)
    if not isinstance(forum, discord.ForumChannel) or forum.guild.id != guild_id:
        raise ValueError("Configured channel is not a forum in the configured guild")
    return forum


async def export_thread(thread, source):
    cap = min(500, max(1, core.cfg_int("BugReviewMessagesPerThread", 100)))
    messages = []
    truncated = False
    if content_available():
        # Latest replies are crucial for reopenings. Include the starter even
        # when the bounded recent-history window no longer contains it.
        recent = []
        # Bound network reads even if the thread contains many bot replies.
        scanned = 0
        scan_limit = cap + 100
        async for m in thread.history(limit=scan_limit):
            scanned += 1
            if not core.bot.user or m.author.id != core.bot.user.id:
                recent.append(m)
                if len(recent) > cap:
                    break
        truncated = len(recent) > cap or scanned >= scan_limit
        recent = recent[:cap]
        if not any(m.id == thread.id for m in recent):
            try:
                recent.append(await thread.fetch_message(thread.id))
            except discord.NotFound:
                pass  # deleted starter, not an empty history
        for m in sorted(recent, key=lambda m: m.id):
            if core.bot.user and m.author.id == core.bot.user.id:
                continue  # our suggestions must not become new report evidence
            messages.append({
                "id": str(m.id), "content": m.content or "",
                "created_at": m.created_at.isoformat(),
                "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                "attachments": [{"filename": a.filename, "url": a.url, "size": a.size} for a in m.attachments],
                "embeds": [e.to_dict() for e in m.embeds],
            })
    report = {
        "id": str(thread.id), "guild_id": str(thread.guild.id), "forum_id": str(thread.parent_id),
        "source": source, "title": thread.name,
        "url": f"https://discord.com/channels/{thread.guild.id}/{thread.id}",
        "archived": thread.archived, "locked": thread.locked,
        "content_available": content_available(), "history_truncated": truncated,
        "messages": messages,
    }
    report["revision"] = revision(report)
    return report


async def export_once():
    guild, internal, public = settings()
    cap = min(1000, max(1, core.cfg_int("BugReviewThreadsPerForum", 100)))
    snapshot = {"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                "content_available": content_available(), "threads": [], "forums": [], "errors": []}
    for fid in sorted(internal) + sorted(public):
        try:
            forum = await resolve_forum(fid, guild)
            active = [t for t in await forum.guild.active_threads() if t.parent_id == fid]
            archived = [t async for t in forum.archived_threads(limit=cap + 1)]
            by_id = {t.id: t for t in active + archived}
            # Prioritize active threads; expose coverage limits rather than
            # implying this is a full archive of the forum.
            ordered = sorted(by_id.values(), key=lambda t: (t.archived, -t.id))
            snapshot["forums"].append({"id": str(fid), "truncated": len(ordered) > cap,
                                       "exported_limit": cap})
            for thread in ordered[:cap]:
                try:
                    snapshot["threads"].append(await export_thread(thread, "internal" if fid in internal else "public"))
                except Exception:
                    snapshot["errors"].append({"thread_id": str(thread.id), "error": "read_failed"})
                    logging.warning("Bug review: could not export thread %s", thread.id)
        except Exception:
            snapshot["errors"].append({"forum_id": str(fid), "error": "read_failed"})
            logging.warning("Bug review: could not export forum %s", fid)
    write_json(root() / "reports.json", snapshot)
    return snapshot


def validate_request(request, filename):
    if not isinstance(request, dict) or request.get("schema") != 1:
        raise ValueError("Unsupported request schema")
    rid = request.get("id", "")
    if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid) or rid != filename:
        raise ValueError("Invalid request ID")
    guild, internal, public = settings()
    try:
        gid, fid, tid = (int(request[k]) for k in ("guild_id", "forum_id", "thread_id"))
    except (KeyError, ValueError, TypeError):
        raise ValueError("Invalid destination") from None
    if gid != guild or fid not in internal or fid in public or tid <= 0:
        raise ValueError("Posting is restricted to the configured internal forums")
    markdown = request.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip() or len(markdown.encode("utf-8")) > MAX_MARKDOWN:
        raise ValueError("Markdown must contain 1 to 65536 UTF-8 bytes")
    if not re.fullmatch(r"[0-9a-f]{64}", str(request.get("report_revision", ""))):
        raise ValueError("Missing report revision")
    return gid, fid, tid


async def publish(request, filename, snapshot, state):
    gid, fid, tid = validate_request(request, filename)
    reports = [r for r in snapshot.get("threads", []) if r["id"] == str(tid)
               and r["forum_id"] == str(fid) and r["guild_id"] == str(gid)]
    if not reports or reports[0]["revision"] != request["report_revision"] or not reports[0]["content_available"]:
        raise ValueError("Missing, stale, or content-restricted report; fetch and investigate again")
    # Resolve actual destination immediately before every write. The request's
    # source label and IDs are never sufficient authorization.
    forum = await resolve_forum(fid, gid)
    if forum.permissions_for(forum.guild.default_role).view_channel:
        raise ValueError("Internal forum is visible to @everyone; refusing to publish")
    thread = await core.bot.fetch_channel(tid)
    if not isinstance(thread, discord.Thread) or thread.guild.id != gid or thread.parent_id != fid:
        raise ValueError("Thread does not belong to the internal forum")
    if thread.archived or thread.locked:
        raise ValueError("Thread is archived or locked; reopen it explicitly before posting")
    current = await export_thread(thread, "internal")
    if not current["content_available"] or current["revision"] != request["report_revision"]:
        raise ValueError("Report changed since investigation; fetch and review new evidence")
    digest = hashlib.sha256(request["markdown"].encode()).hexdigest()
    previous = state["threads"].get(str(tid), {})
    message = None
    if previous.get("message_id"):
        message = await thread.fetch_message(int(previous["message_id"]))
        if not core.bot.user or message.author.id != core.bot.user.id:
            raise ValueError("Stored reply is not owned by this bot")
    if message and previous.get("digest") == digest:
        return {"status": "unchanged", "message_id": previous["message_id"]}
    attachment = discord.File(io.BytesIO(request["markdown"].encode("utf-8")), filename="suggested-fix.md")
    content = "Developer investigation — suggested fix attached. Validation status and limitations are in the document."
    options = {"allowed_mentions": discord.AllowedMentions.none()}
    if message:
        await message.edit(content=content, attachments=[attachment], **options)
    else:
        message = await thread.send(content, file=attachment, **options)
    state["threads"][str(tid)] = {"message_id": str(message.id), "digest": digest}
    return {"status": "posted", "message_id": str(message.id)}


async def process_outbox():
    if core.config.get("BugReviewPostingEnabled", False) is not True:
        return
    path = root()
    snapshot = read_json(path / "reports.json")
    state_path = path / "state.json"
    state = read_json(state_path) if state_path.exists() else {"requests": {}, "threads": {}}
    candidates = (file for file in sorted((path / "outbox").glob("*.json"))
                  if REQUEST_ID.fullmatch(file.stem) and file.is_file() and not file.is_symlink())
    processed = 0
    for file in candidates:
        if processed >= 20:
            break
        processed += 1
        rid = file.stem
        if not REQUEST_ID.fullmatch(rid) or file.is_symlink():
            continue
        if rid in state["requests"]:
            # A restart after Discord accepted a write but before persistence
            # must not silently repeat it. 'uncertain' needs operator inspection.
            write_json(path / "receipts" / file.name, state["requests"][rid])
            file.unlink()
            continue
        try:
            if file.stat().st_size > MAX_MARKDOWN * 6 + 4096:
                raise ValueError("Request too large")
            request = read_json(file)
            validate_request(request, rid)
        except OSError:
            logging.warning("Bug review could not read request %s", rid)
            continue
        except (ValueError, KeyError, TypeError):
            result = {"status": "rejected", "reason": "Invalid request"}
        else:
            state["requests"][rid] = {"status": "uncertain", "reason": "Inspect Discord before resubmitting", "time": time.time()}
            write_json(state_path, state)  # durable claim BEFORE the Discord write
            try:
                result = await publish(request, rid, snapshot, state)
            except ValueError as e:
                result = {"status": "rejected", "reason": str(e)}
            except Exception:
                result = {"status": "uncertain", "reason": "Discord or storage failure; inspect before resubmitting"}
                logging.warning("Bug review submission %s has uncertain status", rid)
        result["time"] = time.time()
        state["requests"][rid] = result
        write_json(state_path, state)
        write_json(path / "receipts" / file.name, result)
        file.unlink()


async def loop():
    await core.bot.wait_until_ready()
    next_scan = 0
    while not core.bot.is_closed():
        if core.config.get("BugReviewEnabled", False) is True:
            try:
                settings()
                if time.monotonic() >= next_scan:
                    await export_once()
                    next_scan = time.monotonic() + max(300, core.cfg_int("BugReviewScanIntervalSec", 900))
                await process_outbox()
            except Exception:
                logging.exception("Bug review bridge failed")
        await asyncio.sleep(30)
