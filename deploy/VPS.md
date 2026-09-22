# Running Barnabus on a VPS

Any small Linux box works: 1 vCPU, 1 GB RAM, a few GB of disk. A Hetzner CX22 or a comparable droplet is plenty. The bot makes only outbound connections; no ports need opening.

```bash
# as root on a fresh Ubuntu 24.04
apt update && apt install -y python3-venv git
useradd -r -m -d /opt/barnabus -s /usr/sbin/nologin barnabus
sudo -u barnabus git clone git@github.com:Raw-Darkness/BarnabusBot.git /opt/barnabus/src
cd /opt/barnabus/src
sudo -u barnabus python3 -m venv /opt/barnabus/venv
sudo -u barnabus /opt/barnabus/venv/bin/pip install -r requirements.txt
```

Copy the private files in from the current machine (they are not in git):

```bash
scp Barnabus.json barnabus.db game_faq.txt world_lore.txt root@VPS:/opt/barnabus/src/
ssh root@VPS chown barnabus:barnabus /opt/barnabus/src/*
```

Install the unit (edit `WorkingDirectory` to `/opt/barnabus/src` if you cloned there):

```bash
cp deploy/barnabusbot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now barnabusbot
journalctl -u barnabusbot -f
```

Updating:

```bash
cd /opt/barnabus/src && sudo -u barnabus git pull && systemctl restart barnabusbot
```

## Moving the database at cutover

`barnabus.db` is a copy of IsabellBot's `isabot.db` taken on 2026-09-22 (same schema). If IsabellBot has run since, copy the file again right before starting Barnabus so no XP or records are lost, then keep only one bot writing to it.

## Backups

The database is a single file. A nightly copy is enough:

```bash
echo '0 4 * * * cp /opt/barnabus/src/barnabus.db /opt/barnabus/barnabus.db.bak' | crontab -u barnabus -
```
