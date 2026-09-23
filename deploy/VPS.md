# Hosting Barnabus on Hetzner Cloud

The bot needs one small always-on Linux server with a persistent disk. It only makes outbound connections, so nothing is exposed except SSH. **Exactly one copy may run at a time**: two copies on the same token would both award XP, both alert and both act on the honeypot.

## Layout on the server

| Path | What |
|---|---|
| `/opt/barnabus/app` | git checkout of this repo, plus the private runtime files (config, database, key, FAQ, lore), which git ignores |
| `/opt/barnabus/venv` | Python environment with the pinned `requirements.txt` |
| `/opt/barnabus/backups` | nightly archives, 14 days |
| `/etc/barnabus/update.env` | optional `ALERT_WEBHOOK=` (Discord webhook) and `ANNOUNCE_DEPLOYS=1` |
| `/etc/barnabus/backup.env` | optional `RESTIC_REPOSITORY=` and `RESTIC_PASSWORD=` for off-server backups |

## How updates reach the server

1. A push to `master` runs the tests in GitHub Actions (`.github/workflows/ci.yml`).
2. Every 5 minutes `barnabus-update.timer` fetches `master`. A new commit is deployed only after its CI run succeeded; a failed run is skipped and reported.
3. The updater installs dependencies if `requirements.txt` changed, imports the package against the example config, restarts the bot and waits up to 90 seconds for `READY`.
4. If any step fails it checks out the previous commit, restarts, marks the bad commit so it is not retried, and posts to `ALERT_WEBHOOK`.

Logs: `journalctl -u barnabus-update` for deploys, `journalctl -u barnabusbot` for the bot.

## Monitoring

Set `HeartbeatURL` in `Barnabus.json` to a healthchecks.io check URL (period 5 min, grace 5 min). The bot pings it every `HeartbeatIntervalSec` while connected to Discord; if the pings stop for any reason — crash, token revoked, server down — healthchecks.io emails or posts to Discord.

## Backups

* Nightly at 03:30: a consistent SQLite copy plus `record.key`, `Barnabus.json`, FAQ and lore, in one archive in `/opt/barnabus/backups`. The key and the database must be restored together or stored excerpts are unreadable.
* Hetzner server backups (enabled by `hetzner_create.py`) keep 7 daily snapshots of the whole disk, stored off the server.
* Optional: restic to a Hetzner Storage Box or Backblaze B2 via `/etc/barnabus/backup.env`.

## First-time setup

```bash
# 1. On your machine: create the server (needs an API token for the project in ~/.config/hcloud-barnabus.token)
deploy/hetzner_create.py --dry-run          # shows type and price
deploy/hetzner_create.py

# 2. On the server, as root:
curl -fsSL https://raw.githubusercontent.com/Raw-Darkness/barnabus-bot/master/deploy/provision.sh | bash

# 3. Cutover — stop the old copy FIRST, then move the data, then start the new one:
sudo systemctl stop barnabusbot && sudo systemctl disable barnabusbot     # on the old machine
scp Barnabus.json barnabus.db record.key game_faq.txt world_lore.txt root@SERVER:/opt/barnabus/app/
ssh root@SERVER 'chown barnabus:barnabus /opt/barnabus/app/{Barnabus.json,barnabus.db,record.key,game_faq.txt,world_lore.txt} && chmod 600 /opt/barnabus/app/{Barnabus.json,record.key} && systemctl start barnabusbot'
```

## Day to day

* Edit FAQ or lore locally, then `deploy/push-content.sh SERVER`. The bot reloads them within 10 seconds.
* Change config: edit `/opt/barnabus/app/Barnabus.json` on the server; it hot-reloads.
* Roll back manually: `cd /opt/barnabus/app && sudo -u barnabus git reset --hard <commit> && systemctl restart barnabusbot`, then add the bad commit to `/var/lib/barnabus-update/rejected` so the updater does not redeploy it.
