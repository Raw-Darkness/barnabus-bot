# Barnabus in a container. Code lives in /app; everything the bot writes or
# edits (config, database, key, FAQ, lore, logs) lives in /data, which must be
# a persistent volume. Run exactly ONE container per bot token.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    BOT_CONFIG=/data/Barnabus.json

RUN apt-get update -q && apt-get install -yq --no-install-recommends sqlite3 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY barnabus ./barnabus
COPY Config.example.json deploy/backup.sh ./

# Relative paths in the config (barnabus.db, record.key, game_faq.txt ...) resolve here.
WORKDIR /data
VOLUME /data

# DISCORD_TOKEN and OPENROUTER_API_KEY may be set as environment secrets instead
# of being stored in Barnabus.json.
CMD ["python", "-m", "barnabus"]
