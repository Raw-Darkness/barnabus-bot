#!/usr/bin/env python3
"""Review encrypted Discord bug reports locally and queue explicit suggestions."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

# Direct invocation as "python tools/bug_review.py" also needs the repository package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from barnabus import review_crypto

CONFIG = Path(__file__).with_name("bug-review-client.json")
HOST = re.compile(r"\A(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
PART = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
ID = re.compile(r"\A[0-9a-f]{32}\Z")
WORD = re.compile(r"\w+", re.UNICODE)
MAX_AGE = 30 * 24 * 60 * 60
RETENTION_MARGIN = 60
SNAPSHOT = "reports.enc.json"


class ReviewError(Exception):
    pass


def _path(value: object, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ReviewError(f"Config {label} must be a nonempty path")
    result = Path(value).expanduser()
    if not result.is_absolute():
        result = base / result
    return result.resolve(strict=False)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def config_from(path: Path, *, cleanup_only: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Cannot read local config: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewError("Config must be an object")
    local_value = value.get("local_dir", "bug-review-local")
    if not isinstance(local_value, str) or not local_value:
        raise ReviewError("Config local_dir must be a nonempty path")
    raw_local = Path(local_value).expanduser()
    if not raw_local.is_absolute():
        raw_local = path.parent / raw_local
    if any(part.is_symlink() for part in (raw_local, *raw_local.parents)):
        raise ReviewError("Config local_dir cannot contain symlinks")
    local = _path(value.get("local_dir", "bug-review-local"), path.parent, "local_dir")
    home = Path.home().resolve()
    repo = Path(__file__).resolve().parents[1]
    anchor = Path(local.anchor)
    if local in (anchor, home, repo) or _inside(home, local) or _inside(repo, local):
        raise ReviewError("Config local_dir must be a dedicated subdirectory, not a root, home, or repository")
    if local.is_symlink():
        raise ReviewError("Config local_dir cannot be a symlink")
    if cleanup_only:
        return {"local_dir": local}
    host = value.get("host", "")
    if not isinstance(host, str) or not HOST.fullmatch(host) or host.startswith("-") or "@-" in host:
        raise ReviewError("Config host must be a nonempty SSH hostname or user@hostname")
    spool = value.get("remote_spool", "/opt/barnabus/app/bug-review")
    if not isinstance(spool, str) or not spool.startswith("/") or not all(
        part not in (".", "..") and PART.fullmatch(part) for part in spool.split("/")[1:]
    ):
        raise ReviewError("Config remote_spool must be an absolute path with simple components")
    private = value.get("private_key")
    public = value.get("public_key")
    private_path = _path(private, path.parent, "private_key") if private else None
    public_path = _path(public, path.parent, "public_key") if public else None
    for key in (private_path, public_path):
        if key is not None and _inside(key, local):
            raise ReviewError("Review keys must be outside local_dir")
    return {"host": host, "remote_spool": spool, "local_dir": local,
            "private_key": private_path, "public_key": public_path,
            "encrypted_drive_confirmed": value.get("encrypted_drive_confirmed") is True}


def _require_private(config: dict) -> None:
    if not config["encrypted_drive_confirmed"]:
        raise ReviewError("Set encrypted_drive_confirmed true only after placing local_dir on an encrypted drive")
    if config["private_key"] is None or not config["private_key"].is_file():
        raise ReviewError("Config private_key must name an existing key outside local_dir")


def _require_public(config: dict) -> None:
    if config["public_key"] is None or not config["public_key"].is_file():
        raise ReviewError("Config public_key must name an existing key outside local_dir")


def transfer(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              input=input_text, encoding="utf-8", timeout=60)
    except FileNotFoundError as exc:
        raise ReviewError(f"Transfer program unavailable: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"{args[0]} transfer timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ReviewError(f"{args[0]} transfer failed (exit {exc.returncode})") from exc


def remote(config: dict, *parts: str) -> str:
    return f"{config['host']}:{config['remote_spool']}/{'/'.join(parts)}"


def _safe_dir(local: Path, name: str) -> Path:
    directory = local / name
    if directory.is_symlink():
        raise ReviewError(f"Managed {name} directory cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _atomic_json(destination: Path, value: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".bug-review-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def download(config: dict, parts: tuple[str, ...], destination: Path) -> Path:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".bug-review-", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            transfer(["scp", "-B", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                      "--", remote(config, *parts), str(temporary)])
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise ReviewError(f"Cannot save downloaded file: {exc}") from exc
    return destination


def _expiry(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReviewError("Invalid review expiry")
    return float(value)


def _read_envelope(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise ValueError("symlink")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) !=
            {"schema", "algorithm", "expires_at", "wrapped_key", "nonce", "ciphertext"} or
            value.get("schema") != review_crypto.SCHEMA or
            value.get("algorithm") != review_crypto.ALGORITHM):
            raise ValueError("envelope")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReviewError("Invalid encrypted review file") from None


def _validate_snapshot(value: dict, envelope_expiry: float, now: float) -> dict:
    if value.get("schema") != 2 or not isinstance(value.get("generated_at"), str):
        raise ReviewError("Local snapshot has an unsupported schema")
    if not isinstance(value.get("content_available"), bool):
        raise ReviewError("Local snapshot is missing content_available")
    if not isinstance(value.get("threads"), list) or not isinstance(value.get("metadata_threads", []), list):
        raise ReviewError("Local snapshot has invalid thread lists")
    expires = _expiry(value.get("expires_at"))
    if expires <= now or expires > envelope_expiry:
        raise ReviewError("Local snapshot has an invalid expiry")
    return value


def snapshot(config: dict, now: float | None = None) -> dict:
    _require_private(config)
    instant = time.time() if now is None else now
    path = config["local_dir"] / SNAPSHOT
    if not path.exists():
        raise ReviewError("Cannot read local snapshot; run fetch first")
    try:
        envelope = _read_envelope(path)
        payload = review_crypto.unseal(envelope, config["private_key"], now=instant)
        return _validate_snapshot(payload, _expiry(envelope.get("expires_at")), instant)
    except (ReviewError, ValueError, OSError):
        _unlink_child(path)
        raise ReviewError("Invalid or expired encrypted snapshot; run fetch again") from None


def threads(value: dict) -> list[dict]:
    result = value["threads"] + value.get("metadata_threads", [])
    for thread in result:
        if not isinstance(thread, dict) or not all(
            isinstance(thread.get(key), str)
            for key in ("id", "guild_id", "forum_id", "source", "title")
        ) or thread["source"] not in ("internal", "public"):
            raise ReviewError("Local snapshot contains an invalid thread")
    for thread in value["threads"]:
        if not isinstance(thread.get("revision"), str):
            raise ReviewError("Local snapshot contains an invalid recent thread")
    return sorted(result, key=lambda t: (t["source"] != "internal", t["title"].casefold(), t["id"]))


def words(value: str) -> set[str]:
    return {match.group().casefold() for match in WORD.finditer(value)}


def text_of(thread: dict) -> str:
    messages = thread.get("messages", [])
    if not isinstance(messages, list) or any(
        not isinstance(message, dict) or not isinstance(message.get("content"), str)
        for message in messages
    ):
        raise ReviewError("Local snapshot contains invalid messages")
    parts = [thread["title"]]
    for message in messages:
        parts.append(message["content"])
        embeds = message.get("embeds", [])
        if not isinstance(embeds, list):
            raise ReviewError("Local snapshot contains invalid embeds")
        for embed in embeds:
            if not isinstance(embed, dict):
                raise ReviewError("Local snapshot contains an invalid embed")
            parts.extend(embed[key] for key in ("title", "description") if isinstance(embed.get(key), str))
            fields = embed.get("fields", [])
            if isinstance(fields, list):
                for field in fields:
                    if isinstance(field, dict):
                        parts.extend(field[key] for key in ("name", "value") if isinstance(field.get(key), str))
            footer = embed.get("footer")
            if isinstance(footer, dict) and isinstance(footer.get("text"), str):
                parts.append(footer["text"])
    return " ".join(parts)


def list_reports(value: dict, query: str | None = None, duplicates: bool = False) -> None:
    ordered = threads(value)
    limitations = []
    if not value["content_available"]:
        limitations.append("message content is unavailable")
    if value.get("metadata_threads"):
        limitations.append("some reports have metadata only")
    if any(thread.get("content_available") is False for thread in ordered):
        limitations.append("some reports have no message content")
    if any(thread.get("history_truncated") for thread in ordered):
        limitations.append("some report histories are truncated")
    if any(isinstance(forum, dict) and forum.get("truncated") for forum in value.get("forums", [])):
        limitations.append("some forums have more threads than exported")
    if value.get("errors"):
        limitations.append("some reports or forums could not be exported")
    if limitations:
        print("warning: snapshot may be incomplete: " + "; ".join(limitations), file=sys.stderr)
    query_words = words(query) if query else set()
    matches = [thread for thread in ordered if query_words <= words(text_of(thread))]
    for thread in matches:
        print(f"[{thread['source']}] {thread['id']}  {thread['title']}")
        if thread.get("url"):
            print(f"  {thread['url']}")
        if duplicates:
            title_words = words(thread["title"])
            scores = []
            if title_words:
                for other in ordered:
                    if other["id"] == thread["id"]:
                        continue
                    other_words = words(other["title"])
                    score = len(title_words & other_words) / len(title_words | other_words)
                    if score:
                        scores.append((score, other))
            for _, other in sorted(scores, key=lambda item: (-item[0], item[1]["id"]))[:3]:
                print(f"  possible duplicate: {other['id']}  {other['title']}")
    print(f"{len(matches)} report(s)")


def _selected(value: dict, thread_id: str, now: float) -> dict:
    if not value["content_available"]:
        raise ReviewError("Snapshot has no message content; suggestions cannot be submitted")
    matches = [thread for thread in value["threads"] if thread.get("id") == thread_id]
    if len(matches) != 1:
        raise ReviewError("Thread ID was not found exactly once among recent reports")
    thread = matches[0]
    if thread["source"] != "internal":
        raise ReviewError("Suggestions may be submitted only for internal reports")
    if thread.get("content_available") is not True:
        raise ReviewError("Selected report has no message content")
    if not all(thread[key] for key in ("id", "guild_id", "forum_id", "revision")):
        raise ReviewError("Selected report is missing its identity or revision")
    if not re.fullmatch(r"[0-9a-f]{64}", thread["revision"]):
        raise ReviewError("Selected report has an invalid revision")
    expiry = _expiry(thread.get("expires_at"))
    if expiry <= now:
        raise ReviewError("Selected report has expired")
    return thread


def _ledger_path(local: Path) -> Path:
    return local / "retention.json"


def _ledger(local: Path) -> dict:
    path = _ledger_path(local)
    if not path.exists():
        return {"schema": 1, "drafts": {}}
    try:
        if path.is_symlink():
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema") != 1 or not isinstance(value.get("drafts"), dict):
            raise ValueError()
        return value
    except (OSError, ValueError, AttributeError, UnicodeError):
        raise ReviewError("Invalid retention ledger; managed drafts cannot be used") from None


def _save_ledger(local: Path, ledger: dict) -> None:
    _atomic_json(_ledger_path(local), ledger)


def _managed_draft(local: Path, filename: Path, ledger: dict, now: float) -> tuple[Path, dict]:
    root = local / "drafts"
    if root.is_symlink() or filename.is_symlink():
        raise ReviewError("Submission draft must be a regular managed file")
    try:
        path = filename.resolve(strict=True)
    except OSError:
        raise ReviewError("Submission draft does not exist") from None
    if path.parent != root.resolve(strict=False) or not path.is_file():
        raise ReviewError("Submission draft must be inside local_dir/drafts")
    entry = ledger["drafts"].get(path.name)
    if not isinstance(entry, dict):
        raise ReviewError("Submission draft is not registered in the retention ledger")
    expiry = _expiry(entry.get("expires_at"))
    if expiry <= now:
        path.unlink(missing_ok=True)
        ledger["drafts"].pop(path.name, None)
        _save_ledger(local, ledger)
        raise ReviewError("Submission draft has expired")
    return path, entry


def create_draft(config: dict, value: dict, thread_id: str, now: float | None = None) -> Path:
    instant = time.time() if now is None else now
    thread = _selected(value, thread_id, instant)
    expiry = min(_expiry(value["expires_at"]), _expiry(thread["expires_at"]), instant + MAX_AGE)
    directory = _safe_dir(config["local_dir"], "drafts")
    ledger = _ledger(config["local_dir"])
    filename = f"{thread_id}-{uuid.uuid4().hex}.md"
    if not re.fullmatch(r"[A-Za-z0-9_-]+-[0-9a-f]{32}\.md", filename):
        raise ReviewError("Invalid thread ID for a managed draft")
    destination = directory / filename
    template = ("# Suggested fix\n\n## Finding\n\n## Evidence and reproduction\n\n"
                "## Proposed change\n\n## Tests and remaining uncertainty\n")
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(template)
        ledger["drafts"][filename] = {"thread_id": thread_id, "revision": thread["revision"],
                                      "created_at": instant, "expires_at": expiry}
        _save_ledger(config["local_dir"], ledger)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ReviewError(f"Cannot create managed draft: {exc}") from exc
    return destination


def build_request(value: dict, thread_id: str, markdown_file: Path, local: Path,
                  ledger: dict, now: float | None = None) -> dict:
    instant = time.time() if now is None else now
    thread = _selected(value, thread_id, instant)
    path, entry = _managed_draft(local, markdown_file, ledger, instant)
    if entry.get("thread_id") != thread_id or entry.get("revision") != thread["revision"]:
        raise ReviewError("Draft source report or revision does not match the current snapshot")
    expiry = min(_expiry(entry["expires_at"]), _expiry(value["expires_at"]),
                 _expiry(thread["expires_at"]), instant + MAX_AGE)
    if expiry <= instant:
        raise ReviewError("Draft source report has expired")
    try:
        data = path.read_bytes()
        markdown = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewError(f"Cannot read Markdown file: {exc}") from exc
    if not markdown.strip() or len(data) > 64 * 1024:
        raise ReviewError("Markdown must be nonempty UTF-8 and at most 64 KiB")
    return {"schema": 1, "id": uuid.uuid4().hex, "thread_id": thread["id"],
            "guild_id": thread["guild_id"], "forum_id": thread["forum_id"],
            "report_revision": thread["revision"], "markdown": markdown,
            "created_at": instant, "expires_at": expiry}


def save_request(config: dict, request: dict) -> Path:
    _require_public(config)
    try:
        envelope = review_crypto.seal(request, config["public_key"], request["expires_at"])
        directory = _safe_dir(config["local_dir"], "submissions")
        destination = directory / f"{request['id']}.enc.json"
        _atomic_json(destination, envelope)
        return destination
    except (OSError, ValueError) as exc:
        raise ReviewError("Cannot encrypt and save local submission") from None


def submit(config: dict, request: dict) -> dict:
    command = ("python3 /opt/barnabus/app/tools/bug_review_send.py "
               + shlex.quote(config["remote_spool"] + "/bridge.sock"))
    response = transfer(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "--",
                         config["host"], command],
                        input_text=json.dumps(request, ensure_ascii=False, separators=(",", ":")))
    try:
        receipt = json.loads(response.stdout)
        if not isinstance(receipt, dict) or receipt.get("status") not in (
            "posted", "unchanged", "rejected", "uncertain"
        ):
            raise ValueError()
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ReviewError("Invalid submission response; check status before retrying") from None
    return receipt


def _unlink_child(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)


def cleanup(config: dict, now: float | None = None) -> None:
    """Remove expired or unregistered managed files, without following symlinks."""
    instant = time.time() if now is None else now
    local = config["local_dir"]
    if local.is_symlink():
        raise ReviewError("Managed local_dir cannot be a symlink")
    if not local.exists():
        return
    if not local.is_dir():
        raise ReviewError("Managed local_dir is not a directory")
    for path in local.iterdir():
        if path.name.startswith(".bug-review-") and (path.is_file() or path.is_symlink()):
            try:
                if path.is_symlink() or path.stat().st_mtime + 3600 <= instant:
                    _unlink_child(path)
            except OSError:
                _unlink_child(path)
    _unlink_child(local / "reports.json")
    report = local / SNAPSHOT
    if report.exists() or report.is_symlink():
        try:
            envelope = _read_envelope(report)
            expiry = _expiry(envelope.get("expires_at"))
            if expiry <= instant + RETENTION_MARGIN or expiry > instant + MAX_AGE or report.stat().st_mtime + MAX_AGE <= instant:
                _unlink_child(report)
        except (ReviewError, OSError):
            _unlink_child(report)
    ledger_path = _ledger_path(local)
    try:
        ledger = _ledger(local)
    except ReviewError:
        _unlink_child(ledger_path)
        ledger = {"schema": 1, "drafts": {}}
    drafts = local / "drafts"
    if drafts.is_symlink():
        drafts.unlink()
    elif drafts.is_dir():
        for path in drafts.iterdir():
            entry = ledger["drafts"].get(path.name)
            try:
                expiry = _expiry(entry.get("expires_at")) if isinstance(entry, dict) else 0
                first = _expiry(entry.get("created_at")) if isinstance(entry, dict) else 0
                valid = (path.is_file() and not path.is_symlink() and expiry > instant + RETENTION_MARGIN
                         and first <= instant and first + MAX_AGE > instant + RETENTION_MARGIN
                         and path.stat().st_mtime + MAX_AGE > instant + RETENTION_MARGIN)
            except (ReviewError, OSError):
                valid = False
            if not valid:
                _unlink_child(path)
                ledger["drafts"].pop(path.name, None)
    for name in list(ledger["drafts"]):
        if not (drafts / name).is_file() or (drafts / name).is_symlink():
            ledger["drafts"].pop(name, None)
    if ledger_path.exists() or ledger["drafts"]:
        _save_ledger(local, ledger)
    submissions = local / "submissions"
    if submissions.is_symlink():
        submissions.unlink()
    elif submissions.is_dir():
        for path in submissions.iterdir():
            try:
                envelope = _read_envelope(path)
                expiry = _expiry(envelope.get("expires_at"))
                valid = (path.name.endswith(".enc.json") and expiry > instant + RETENTION_MARGIN
                         and expiry <= instant + MAX_AGE
                         and path.stat().st_mtime + MAX_AGE > instant + RETENTION_MARGIN)
            except (ReviewError, OSError):
                valid = False
            if not valid:
                _unlink_child(path)
    # Receipts are fetched on demand. Remove files left by earlier client versions.
    receipts = local / "receipts"
    if receipts.is_symlink():
        receipts.unlink()
    elif receipts.is_dir():
        for path in receipts.iterdir():
            _unlink_child(path)


def status(config: dict, request_id: str) -> None:
    if not ID.fullmatch(request_id):
        raise ReviewError("Request ID must be 32 lowercase hexadecimal characters")
    local = config["local_dir"]
    local.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".bug-review-receipt-", dir=local)
    os.close(fd)
    temporary = Path(name)
    try:
        transfer(["scp", "-B", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                  "--", remote(config, "receipts", f"{request_id}.json"), str(temporary)])
        print(temporary.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ReviewError("Cannot read request receipt") from None
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG, help="Local JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="Download encrypted reports.enc.json")
    commands.add_parser("list", help="List local reports")
    search = commands.add_parser("search", help="Search local title and message text")
    search.add_argument("query")
    search.add_argument("--duplicates", action="store_true", help="Show title overlap candidates")
    show = commands.add_parser("show", help="Print one decrypted report to stdout")
    show.add_argument("thread_id")
    draft = commands.add_parser("draft", help="Create a managed suggestion draft")
    draft.add_argument("thread_id")
    suggest = commands.add_parser("submit", help="Queue a managed Markdown draft for an internal report")
    suggest.add_argument("thread_id")
    suggest.add_argument("markdown_file", type=Path)
    receipt = commands.add_parser("status", help="Read a request receipt")
    receipt.add_argument("request_id")
    commands.add_parser("cleanup", help="Purge expired managed files (for OS scheduler)")
    args = parser.parse_args(argv)
    try:
        cleanup(config_from(args.config, cleanup_only=True))
        if args.command == "cleanup":
            return 0
        config = config_from(args.config)
        if args.command == "fetch":
            _require_private(config)
            path = download(config, (SNAPSHOT,), config["local_dir"] / SNAPSHOT)
            snapshot(config)
            print(path)
        elif args.command in ("list", "search"):
            list_reports(snapshot(config), getattr(args, "query", None),
                         getattr(args, "duplicates", False))
        elif args.command == "show":
            value = snapshot(config)
            matches = [thread for thread in threads(value) if thread["id"] == args.thread_id]
            if len(matches) != 1:
                raise ReviewError("Thread ID was not found exactly once")
            print(json.dumps(matches[0], ensure_ascii=False, indent=2))
        elif args.command == "draft":
            print(create_draft(config, snapshot(config), args.thread_id))
        elif args.command == "submit":
            value = snapshot(config)
            request = build_request(value, args.thread_id, args.markdown_file,
                                    config["local_dir"], _ledger(config["local_dir"]))
            save_request(config, request)
            print(request["id"], flush=True)
            try:
                receipt = submit(config, request)
            except ReviewError as exc:
                raise ReviewError(f"{exc}; check status {request['id']} before retrying") from exc
            print(json.dumps(receipt, ensure_ascii=False))
            if receipt["status"] in ("rejected", "uncertain"):
                raise ReviewError(f"Submission {receipt['status']}; inspect status {request['id']}")
        elif args.command == "status":
            status(config, args.request_id)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
