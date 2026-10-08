"""Private file bridge. Discord data is input, never instructions or executable code.

Only the running bot talks to Discord. Owner-operated SSH transfers snapshots and
explicit Markdown submissions; no Unreal files or model calls live on the VPS.
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import stat
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import discord

from . import core, review_crypto
from .review_retention import purge_server, WINDOW

MAX_MARKDOWN = 64 * 1024
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_REQUEST_BYTES = MAX_MARKDOWN * 6 + 4096
_snapshot_index = {"threads": []}
_submit_lock = None


def settings():
    guild = core.cfg_int("BugReviewGuildID")
    internal = core.cfg_ids("BugReviewInternalForumIDs")
    public = core.cfg_ids("BugReviewPublicForumIDs")
    if not guild or not internal or internal & public or any(i <= 0 for i in internal | public):
        raise ValueError("Configure a guild and disjoint internal/public forum lists")
    return guild, internal, public


def root():
    path = Path(core.config.get("BugReviewDirectory") or "bug-review")
    shared = core.config.get("BugReviewSharedAccess", False) is True
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shared:
        # The operator assigns a dedicated group before enabling shared access.
        # setgid keeps atomic replacement files in that group.
        path.chmod(0o2750)
    for name in ("receipts",):
        child = path / name
        child.mkdir(exist_ok=True, mode=0o700)
        if shared:
            child.chmod(0o2750)
    return path


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    shared = core.config.get("BugReviewSharedAccess", False) is True
    exported = path.name == "reports.enc.json" or path.parent.name == "receipts"
    temp.chmod(0o640 if shared and exported else 0o600)
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
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    if content_available():
        # Latest replies are crucial for reopenings. Include the starter even
        # when the bounded recent-history window no longer contains it.
        recent = []
        # Bound network reads even if the thread contains many bot replies.
        scanned = 0
        scan_limit = cap + 100
        async for m in thread.history(limit=scan_limit, after=cutoff, oldest_first=False):
            scanned += 1
            if cutoff < m.created_at <= now and (not core.bot.user or m.author.id != core.bot.user.id):
                recent.append(m)
                if len(recent) > cap:
                    break
        truncated = len(recent) > cap or scanned >= scan_limit
        recent = recent[:cap]
        if not any(m.id == thread.id for m in recent):
            try:
                starter = await thread.fetch_message(thread.id)
                if cutoff < starter.created_at <= now:
                    recent.append(starter)
            except discord.NotFound:
                pass  # deleted starter, not an empty history
        for m in sorted(recent, key=lambda m: m.id):
            if not cutoff < m.created_at <= now or (core.bot.user and m.author.id == core.bot.user.id):
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
        "content_available": content_available() and bool(messages), "history_truncated": truncated,
        "expires_at": min([datetime.fromisoformat(m["created_at"]).timestamp() + WINDOW
                           for m in messages], default=now.timestamp() + WINDOW),
        "messages": messages,
    }
    report["revision"] = revision(report)
    return report


async def export_once():
    guild, internal, public = settings()
    cap = min(1000, max(1, core.cfg_int("BugReviewThreadsPerForum", 100)))
    global _snapshot_index
    public_key = Path(core.config.get("BugReviewPublicKeyPath") or "")
    if not public_key.is_file():
        raise ValueError("Configure the workstation export public key before enabling collection")
    purge_server(core.config.get("BugReviewDirectory") or "bug-review")
    snapshot = {"schema": 2, "generated_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": time.time() + WINDOW,
                "content_available": content_available(), "threads": [], "metadata_threads": [],
                "forums": [], "errors": []}
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
                    report = await export_thread(thread, "internal" if fid in internal else "public")
                    if report["messages"] and report["expires_at"] > time.time() + 60:
                        snapshot["threads"].append(report)
                        snapshot["expires_at"] = min(snapshot["expires_at"], report["expires_at"])
                    else:
                        # No recent content: keep only the permitted metadata.
                        snapshot["metadata_threads"].append({k: report[k] for k in
                            ("id", "guild_id", "forum_id", "source", "title", "url", "archived", "locked")})
                except Exception:
                    snapshot["errors"].append({"thread_id": str(thread.id), "error": "read_failed"})
                    logging.warning("Bug review: could not export thread %s", thread.id)
        except Exception:
            snapshot["errors"].append({"forum_id": str(fid), "error": "read_failed"})
            logging.warning("Bug review: could not export forum %s", fid)
    envelope = review_crypto.seal(snapshot, public_key, snapshot["expires_at"])
    write_json(root() / "reports.enc.json", envelope)
    # Retain only identity/revision/expiry in memory for submission checks.
    _snapshot_index = {"threads": [{k: r[k] for k in
                       ("id", "guild_id", "forum_id", "revision", "content_available", "expires_at")}
                      for r in snapshot["threads"]]}
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
    now = time.time()
    created, expires = request.get("created_at"), request.get("expires_at")
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (created, expires)) or not created <= now < expires
            or expires > created + WINDOW):
        raise ValueError("Submission retention deadline is invalid or expired")
    return gid, fid, tid


async def publish(request, filename, snapshot, state):
    gid, fid, tid = validate_request(request, filename)
    reports = [r for r in snapshot.get("threads", []) if r["id"] == str(tid)
               and r["forum_id"] == str(fid) and r["guild_id"] == str(gid)]
    if (not reports or reports[0]["revision"] != request["report_revision"]
            or not reports[0]["content_available"] or reports[0]["expires_at"] <= time.time()
            or request["expires_at"] > reports[0]["expires_at"]):
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
    # Discord fetches may take time; never publish an expired request.
    validate_request(request, filename)
    if message:
        await message.edit(content=content, attachments=[attachment], **options)
    else:
        message = await thread.send(content, file=attachment, **options)
    state["threads"][str(tid)] = {"message_id": str(message.id), "digest": digest}
    return {"status": "posted", "message_id": str(message.id)}


async def submit_request(request):
    """Consume a suggestion in memory. Only content-free receipts reach disk."""
    if (core.config.get("BugReviewEnabled", False) is not True
            or core.config.get("BugReviewPostingEnabled", False) is not True):
        return {"status": "rejected", "reason": "Posting disabled"}
    rid = request.get("id", "") if isinstance(request, dict) else ""
    try:
        validate_request(request, rid)
    except (ValueError, TypeError, KeyError):
        return {"status": "rejected", "reason": "Invalid or expired request"}
    path = root()
    state_path = path / "state.json"
    state = read_json(state_path) if state_path.exists() else {"requests": {}, "threads": {}}
    if rid in state["requests"]:
        return state["requests"][rid]
    state["requests"][rid] = {"status": "uncertain", "reason": "Inspect Discord before resubmitting", "time": time.time()}
    write_json(state_path, state)
    try:
        result = await publish(request, rid, _snapshot_index, state)
    except ValueError as e:
        result = {"status": "rejected", "reason": str(e)}
    except Exception:
        result = {"status": "uncertain", "reason": "Discord or storage failure; inspect before resubmitting"}
        logging.warning("Bug review submission %s has uncertain status", rid)
    result["time"] = time.time()
    state["requests"][rid] = result
    write_json(state_path, state)
    write_json(path / "receipts" / (rid + ".json"), result)
    return result


async def receive_submission(reader, writer):
    global _submit_lock
    try:
        if _submit_lock is None:
            _submit_lock = asyncio.Lock()
        if _submit_lock.locked():
            result = {"status": "rejected", "reason": "Another submission is processing; try later"}
        else:
            async with _submit_lock:
                line = await asyncio.wait_for(reader.readline(), timeout=15)
                if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                    raise ValueError("Invalid request frame")
                request = json.loads(line.decode("utf-8"))
                result = await asyncio.wait_for(submit_request(request), timeout=45)
        writer.write(json.dumps(result).encode() + b"\n")
        await writer.drain()
    except (ValueError, UnicodeError, asyncio.TimeoutError):
        # Do not log decoder tracebacks: their messages may contain report text.
        writer.write(b'{"status":"uncertain","reason":"Invalid request or timeout; check receipt"}\n')
        await writer.drain()
    except Exception:
        logging.warning("Bug review submission transport failed")
    finally:
        writer.close()
        await writer.wait_closed()


async def start_listener():
    path = root() / "bridge.sock"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):
            raise ValueError("Unsafe bridge socket path")
        try:
            _, writer = await asyncio.open_unix_connection(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink(missing_ok=True)
        else:
            writer.close()
            await writer.wait_closed()
            raise ValueError("Another bridge listener is already running")
    server = await asyncio.start_unix_server(receive_submission, path=str(path), limit=MAX_REQUEST_BYTES+1)
    path.chmod(0o660 if core.config.get("BugReviewSharedAccess", False) is True else 0o600)
    return server


async def loop():
    await core.bot.wait_until_ready()
    next_scan, failed_config, server, active_directory = 0, None, None, None
    try:
        while not core.bot.is_closed():
            directory = core.config.get("BugReviewDirectory") or "bug-review"
            signature = json.dumps({k: v for k, v in core.config.items() if k.startswith("BugReview")}, sort_keys=True)
            try:
                purge_server(directory)  # also runs when disabled or misconfigured
                enabled = core.config.get("BugReviewEnabled", False) is True
                if server and (not enabled or directory != active_directory):
                    server.close()
                    await server.wait_closed()
                    server = None
                    next_scan = 0
                if enabled:
                    settings()
                    if signature != failed_config:
                        next_scan = 0 if failed_config is not None else next_scan
                    if time.monotonic() >= next_scan:
                        await export_once()
                        next_scan = time.monotonic() + max(300, core.cfg_int("BugReviewScanIntervalSec", 900))
                    if server is None and os.name != "nt":
                        server = await start_listener()
                        active_directory = directory
                failed_config = None
            except Exception:
                if failed_config != signature:
                    logging.warning("Bug review unavailable; check forum IDs, public key, and filesystem setup (details omitted)")
                    failed_config = signature
            await asyncio.sleep(30)
    finally:
        if server:
            server.close()
            await server.wait_closed()
