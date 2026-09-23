#!/bin/bash
# Nightly: a consistent copy of the database plus the key, config and content,
# kept for 14 days. The key is useless without the database and vice versa, so
# they travel together. Off-server copies come from Hetzner's server backups,
# and from restic too when /etc/barnabus/backup.env sets RESTIC_REPOSITORY.
set -euo pipefail
APP=/opt/barnabus/app
D=/opt/barnabus/backups
umask 077
mkdir -p "$D"
ts=$(date +%Y%m%d-%H%M)
work=$(mktemp -d)
sqlite3 "$APP/barnabus.db" ".backup '$work/barnabus.db'"
for f in record.key Barnabus.json game_faq.txt world_lore.txt; do
    [ -f "$APP/$f" ] && cp "$APP/$f" "$work/"
done
tar -czf "$D/barnabus-$ts.tar.gz" -C "$work" .
rm -rf "$work"
find "$D" -name 'barnabus-*.tar.gz' -mtime +14 -delete
echo "backup: $D/barnabus-$ts.tar.gz"

if [ -f /etc/barnabus/backup.env ]; then
    set -a; . /etc/barnabus/backup.env; set +a
    if [ -n "${RESTIC_REPOSITORY:-}" ]; then
        restic backup -q "$D/barnabus-$ts.tar.gz" && restic forget -q --keep-daily 14 --keep-weekly 8 --prune
    fi
fi
