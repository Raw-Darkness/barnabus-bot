#!/bin/bash
# One-time setup of a fresh Ubuntu 24.04 server for Barnabus. Safe to re-run.
# Leaves the bot enabled but NOT started: copy the data in first (see VPS.md).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get upgrade -yq
apt-get install -yq git python3-venv sqlite3 ufw unattended-upgrades restic curl

# Service user and code
id barnabus >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/barnabus --shell /usr/sbin/nologin barnabus
if [ ! -d /opt/barnabus/app/.git ]; then
    runuser -u barnabus -- git clone -q https://github.com/Raw-Darkness/barnabus-bot.git /opt/barnabus/app
fi
[ -x /opt/barnabus/venv/bin/python ] || runuser -u barnabus -- python3 -m venv /opt/barnabus/venv
runuser -u barnabus -- /opt/barnabus/venv/bin/pip install -q --upgrade pip
runuser -u barnabus -- /opt/barnabus/venv/bin/pip install -q -r /opt/barnabus/app/requirements.txt
chmod 755 /opt/barnabus/app/deploy/*.sh
mkdir -p /etc/barnabus && chmod 700 /etc/barnabus

# Units
for u in barnabusbot.service barnabus-update.service barnabus-update.timer barnabus-backup.service barnabus-backup.timer; do
    install -m 644 "/opt/barnabus/app/deploy/$u" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable barnabusbot.service barnabus-update.timer barnabus-backup.timer
systemctl start barnabus-backup.timer barnabus-update.timer

# Firewall: nothing inbound but SSH. The bot only makes outbound connections.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# SSH: keys only — but only once a key is actually installed, or we lock ourselves out.
if [ -s /root/.ssh/authorized_keys ]; then
    cat > /etc/ssh/sshd_config.d/10-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
    systemctl reload ssh
fi

# Security updates daily, reboot at 04:15 when a kernel update needs it.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
cat > /etc/apt/apt.conf.d/52barnabus <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:15";
CONF

# Keep logs across reboots, capped.
mkdir -p /var/log/journal /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=300M\n' > /etc/systemd/journald.conf.d/barnabus.conf
systemctl restart systemd-journald

echo "provisioned. Next: copy Barnabus.json, barnabus.db, record.key, game_faq.txt, world_lore.txt into /opt/barnabus/app, then: systemctl start barnabusbot"
