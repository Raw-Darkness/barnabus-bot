"""Content-free cleanup usable without importing the bot or opening its config."""
import json
import math
import time
from pathlib import Path

WINDOW = 30 * 24 * 60 * 60


def purge_server(directory, now=None):
    now = time.time() if now is None else now
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        return 0
    removed = 0
    # Never recursively traverse a user-supplied tree. These are bridge-owned
    # files only; the state ledger/receipts carry no report text and stay put.
    candidates = [root/'reports.json', root/'reports.tmp', root/'reports.enc.json']
    old_queue = root/'outbox'
    if old_queue.is_dir() and not old_queue.is_symlink():
        candidates.extend(p for p in old_queue.iterdir() if p.suffix in {'.json', '.tmp'})
    for file in candidates:
        if file.is_symlink() or not file.is_file():
            continue
        delete = file.name != 'reports.enc.json'
        if not delete:
            try:
                envelope = json.loads(file.read_text(encoding='utf-8'))
                expiry = envelope['expires_at']
                delete = (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                          or not math.isfinite(expiry) or expiry <= now + 60
                          or expiry > now + WINDOW or file.stat().st_mtime <= now-WINDOW)
            except (ValueError, KeyError, TypeError):
                delete = True
        if delete:
            file.unlink(missing_ok=True)
            removed += 1
    temporary = root/'reports.enc.tmp'
    if temporary.is_file() and not temporary.is_symlink() and temporary.stat().st_mtime < now-300:
        temporary.unlink()
        removed += 1
    return removed
