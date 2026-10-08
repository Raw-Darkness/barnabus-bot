#!/usr/bin/env python3
"""Expire bridge files without importing the bot or using its credentials."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from barnabus.review_retention import purge_server


def resolve_directory(config_path):
    # Only the directory setting is used; never log configuration or parser errors.
    try:
        data = json.loads(Path(config_path).read_text(encoding='utf-8'))
        directory = data.get('BugReviewDirectory') or 'bug-review'
        if not isinstance(directory, str):
            raise ValueError()
        return Path(directory)
    except (OSError, ValueError, AttributeError):
        raise ValueError('Cannot read the bridge directory setting') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--directory', type=Path)
    source.add_argument('--config', type=Path)
    args = parser.parse_args()
    try:
        directory = args.directory if args.directory is not None else resolve_directory(args.config)
        print('Removed expired/legacy bridge files:', purge_server(directory))
        return 0
    except (OSError, ValueError):
        print('Bridge retention cleanup failed; check its configuration and filesystem access.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
