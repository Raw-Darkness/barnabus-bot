#!/usr/bin/env python3
"""Create (or find) the Barnabus server in a Hetzner Cloud project. Safe to re-run.

Reads an API token from $HCLOUD_TOKEN_FILE (default ~/.config/hcloud-barnabus.token).
Creates: the SSH key, a firewall allowing only SSH and ping inbound, the cheapest
current shared x86 server with at least 2 GB RAM in the chosen location, running
Ubuntu 24.04, with Hetzner's daily server backups enabled. Prints the IPv4.

    deploy/hetzner_create.py [--location hel1] [--pubkey ~/.ssh/id_ed25519.pub] [--dry-run]
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.hetzner.cloud/v1"
NAME = "barnabus"
LABELS = {"app": "barnabus"}


def call(token, method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"Hetzner API {method} {path} -> {e.code}: {e.read().decode()[:400]}")


def pick_type(token, location):
    types = call(token, "GET", "/server_types?per_page=50")["server_types"]
    cands = []
    for t in types:
        if t.get("architecture") != "x86" or t.get("cpu_type") != "shared" or t.get("memory", 0) < 2:
            continue
        if t.get("deprecation") or t.get("deprecated"):
            continue
        price = next((p for p in t.get("prices", []) if p.get("location") == location), None)
        if price:
            cands.append((float(price["price_monthly"]["gross"]), t["name"], t["cores"], t["memory"], t["disk"]))
    if not cands:
        sys.exit(f"No suitable server type available in {location}")
    return sorted(cands)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="hel1", help="hel1 Helsinki, fsn1 Falkenstein, nbg1 Nuremberg")
    ap.add_argument("--pubkey", default=os.path.expanduser("~/.ssh/id_ed25519.pub"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tok_file = os.path.expanduser(os.environ.get("HCLOUD_TOKEN_FILE", "~/.config/hcloud-barnabus.token"))
    token = open(tok_file).read().strip()

    existing = call(token, "GET", f"/servers?name={NAME}")["servers"]
    if existing:
        s = existing[0]
        print(f"exists: {NAME} {s['server_type']['name']} {s['public_net']['ipv4']['ip']} status={s['status']}")
        return

    price, stype, cores, mem, disk = pick_type(token, a.location)
    print(f"plan: {stype} ({cores} vCPU, {mem:g} GB RAM, {disk} GB disk) in {a.location}, "
          f"€{price:.2f}/month + 20% for backups, Ubuntu 24.04")
    if a.dry_run:
        return

    pub = open(a.pubkey).read().strip()
    keys = call(token, "GET", "/ssh_keys")["ssh_keys"]
    key = next((k for k in keys if k["public_key"].split()[:2] == pub.split()[:2]), None)
    if key is None:
        key = call(token, "POST", "/ssh_keys", {"name": "barnabus-deploy", "public_key": pub, "labels": LABELS})["ssh_key"]

    fws = call(token, "GET", f"/firewalls?name={NAME}")["firewalls"]
    if fws:
        fw = fws[0]
    else:
        fw = call(token, "POST", "/firewalls", {"name": NAME, "labels": LABELS, "rules": [
            {"direction": "in", "protocol": "tcp", "port": "22", "source_ips": ["0.0.0.0/0", "::/0"], "description": "ssh"},
            {"direction": "in", "protocol": "icmp", "source_ips": ["0.0.0.0/0", "::/0"], "description": "ping"},
        ]})["firewall"]

    res = call(token, "POST", "/servers", {
        "name": NAME, "server_type": stype, "image": "ubuntu-24.04", "location": a.location,
        "ssh_keys": [key["id"]], "firewalls": [{"firewall": fw["id"]}], "labels": LABELS,
        "public_net": {"enable_ipv4": True, "enable_ipv6": True},
    })
    sid = res["server"]["id"]
    for _ in range(60):
        s = call(token, "GET", f"/servers/{sid}")["server"]
        if s["status"] == "running":
            break
        time.sleep(3)
    call(token, "POST", f"/servers/{sid}/actions/enable_backup", {})
    print(f"created: {NAME} {stype} {s['public_net']['ipv4']['ip']} status={s['status']} backups=enabled")


if __name__ == "__main__":
    main()
