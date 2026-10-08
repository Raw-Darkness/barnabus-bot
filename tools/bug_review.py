#!/usr/bin/env python3
"""Review exported Discord bug reports locally and queue explicit suggestions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

CONFIG = Path(__file__).with_name("bug-review-client.json")
HOST = re.compile(r"\A(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
PART = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
ID = re.compile(r"\A[0-9a-f]{32}\Z")
WORD = re.compile(r"\w+", re.UNICODE)


class ReviewError(Exception):
    pass


def config_from(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Cannot read local config: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewError("Config must be an object")
    host = value.get("host", "")
    if not isinstance(host, str) or not HOST.fullmatch(host) or host.startswith("-") or "@-" in host:
        raise ReviewError("Config host must be a nonempty SSH hostname or user@hostname")
    spool = value.get("remote_spool", "/opt/barnabus/app/bug-review")
    if not isinstance(spool, str) or not spool.startswith("/") or not all(
        part not in (".", "..") and PART.fullmatch(part) for part in spool.split("/")[1:]
    ):
        raise ReviewError("Config remote_spool must be an absolute path with simple components")
    local = value.get("local_dir", "bug-review-local")
    if not isinstance(local, str) or not local:
        raise ReviewError("Config local_dir must be a nonempty path")
    local_path = Path(local).expanduser()
    if not local_path.is_absolute():
        local_path = path.parent / local_path
    return {"host": host, "remote_spool": spool, "local_dir": local_path}


def transfer(args: list[str]) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise ReviewError(f"Transfer program unavailable: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"{args[0]} transfer timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ReviewError(f"{args[0]} transfer failed (exit {exc.returncode})") from exc


def remote(config: dict, *parts: str) -> str:
    return f"{config['host']}:{config['remote_spool']}/{'/'.join(parts)}"


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


def snapshot(local_dir: Path) -> dict:
    try:
        value = json.loads((local_dir / "reports.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Cannot read local snapshot; run fetch first: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("threads"), list):
        raise ReviewError("Local snapshot has an unsupported schema")
    if not isinstance(value.get("content_available"), bool):
        raise ReviewError("Local snapshot is missing content_available")
    return value


def threads(value: dict) -> list[dict]:
    result = value["threads"]
    for thread in result:
        if not isinstance(thread, dict) or not all(
            isinstance(thread.get(key), str)
            for key in ("id", "guild_id", "forum_id", "source", "title", "revision")
        ) or thread["source"] not in ("internal", "public"):
            raise ReviewError("Local snapshot contains an invalid thread")
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
    if any(thread.get("content_available") is False for thread in ordered):
        limitations.append("some reports have no message content")
    if any(thread.get("history_truncated") for thread in ordered):
        limitations.append("some report histories are truncated")
    if any(isinstance(forum, dict) and forum.get("truncated")
           for forum in value.get("forums", [])):
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


def build_request(value: dict, thread_id: str, markdown_file: Path) -> dict:
    if not value["content_available"]:
        raise ReviewError("Snapshot has no message content; suggestions cannot be submitted")
    matches = [thread for thread in threads(value) if thread["id"] == thread_id]
    if len(matches) != 1:
        raise ReviewError("Thread ID was not found exactly once in the local snapshot")
    thread = matches[0]
    if thread["source"] != "internal":
        raise ReviewError("Suggestions may be submitted only for internal reports")
    if thread.get("content_available") is not True:
        raise ReviewError("Selected report has no message content")
    if not all(thread[key] for key in ("id", "guild_id", "forum_id", "revision")):
        raise ReviewError("Selected report is missing its identity or revision")
    if not re.fullmatch(r"[0-9a-f]{64}", thread["revision"]):
        raise ReviewError("Selected report has an invalid revision")
    try:
        data = markdown_file.read_bytes()
        markdown = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewError(f"Cannot read Markdown file: {exc}") from exc
    if not markdown.strip() or len(data) > 64 * 1024:
        raise ReviewError("Markdown must be nonempty UTF-8 and at most 64 KiB")
    return {
        "schema": 1, "id": uuid.uuid4().hex, "thread_id": thread["id"],
        "guild_id": thread["guild_id"], "forum_id": thread["forum_id"],
        "report_revision": thread["revision"], "markdown": markdown,
    }


def submit(config: dict, request: dict) -> None:
    request_id = request["id"]
    outbox = f"{config['remote_spool']}/outbox"
    temporary = f"{outbox}/{request_id}.json.tmp"
    final = f"{outbox}/{request_id}.json"
    try:
        with tempfile.TemporaryDirectory(prefix="bug-review-") as directory:
            source = Path(directory) / f"{request_id}.json"
            source.write_text(json.dumps(request, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            transfer(["scp", "-B", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                      "--", str(source), f"{config['host']}:{temporary}"])
    except OSError as exc:
        raise ReviewError(f"Cannot prepare upload: {exc}") from exc
    transfer(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "--", config["host"],
              f"mv -- {shlex.quote(temporary)} {shlex.quote(final)}"])


def save_request(local_dir: Path, request: dict) -> Path:
    submissions = local_dir / "submissions"
    try:
        submissions.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".bug-review-", dir=submissions)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(request, stream, ensure_ascii=False, indent=2)
            destination = submissions / f"{request['id']}.json"
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise ReviewError(f"Cannot save local request: {exc}") from exc
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG, help="Local JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="Download reports.json")
    commands.add_parser("list", help="List local reports")
    search = commands.add_parser("search", help="Search local title and message text")
    search.add_argument("query")
    search.add_argument("--duplicates", action="store_true", help="Show title overlap candidates")
    suggest = commands.add_parser("submit", help="Queue Markdown suggestion for internal report")
    suggest.add_argument("thread_id")
    suggest.add_argument("markdown_file", type=Path)
    status = commands.add_parser("status", help="Download a request receipt")
    status.add_argument("request_id")
    args = parser.parse_args(argv)
    try:
        config = config_from(args.config)
        if args.command == "fetch":
            print(download(config, ("reports.json",), config["local_dir"] / "reports.json"))
        elif args.command in ("list", "search"):
            list_reports(snapshot(config["local_dir"]), getattr(args, "query", None),
                         getattr(args, "duplicates", False))
        elif args.command == "submit":
            request = build_request(snapshot(config["local_dir"]), args.thread_id, args.markdown_file)
            save_request(config["local_dir"], request)
            print(request["id"], flush=True)
            try:
                submit(config, request)
            except ReviewError as exc:
                raise ReviewError(f"{exc}; check status {request['id']} before retrying") from exc
        elif args.command == "status":
            if not ID.fullmatch(args.request_id):
                raise ReviewError("Request ID must be 32 lowercase hexadecimal characters")
            destination = config["local_dir"] / "receipts" / f"{args.request_id}.json"
            path = download(config, ("receipts", f"{args.request_id}.json"), destination)
            print(path.read_text(encoding="utf-8"))
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
