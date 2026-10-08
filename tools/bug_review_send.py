#!/usr/bin/env python3
"""Forward one SSH-stdin submission to the bot's local socket, never to disk."""
import argparse
import json
import socket
import sys

MAX_BYTES = 64 * 1024 * 6 + 4096


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('socket_path')
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError('request size')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('request type')
        wire = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(wire) > MAX_BYTES:
            raise ValueError('request size')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(55)
            connection.connect(args.socket_path)
            connection.sendall(wire)
            response = b''
            while not response.endswith(b'\n') and len(response) < 8192:
                chunk = connection.recv(8192-len(response))
                if not chunk:
                    break
                response += chunk
        receipt = json.loads(response)
        if not isinstance(receipt, dict) or receipt.get('status') not in {'posted','unchanged','rejected','uncertain'}:
            raise ValueError('response type')
        print(json.dumps(receipt))
        return 0
    except Exception:
        # Never print stdin, a JSON exception, or command arguments containing data.
        print('Submission transport failed; inspect the saved request receipt before retrying.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
