#!/bin/bash
# Run on your own machine: upload edited FAQ/lore to the server. The bot
# hot-reloads them within 10 seconds; no restart needed.
#   deploy/push-content.sh [host]      (default host: $BARNABUS_HOST)
set -euo pipefail
HOST=${1:-${BARNABUS_HOST:?set BARNABUS_HOST or pass the server address}}
files=()
for f in game_faq.txt world_lore.txt; do [ -f "$f" ] && files+=("$f"); done
[ ${#files[@]} -gt 0 ] || { echo "no game_faq.txt or world_lore.txt here"; exit 1; }
scp -q "${files[@]}" "root@$HOST:/tmp/"
ssh "root@$HOST" "cd /tmp && install -o barnabus -g barnabus -m 640 ${files[*]} /opt/barnabus/app/ && rm -f ${files[*]}"
echo "pushed: ${files[*]}"
