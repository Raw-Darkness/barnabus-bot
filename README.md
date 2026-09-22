# Barnabus

Moderation and community bot for a Discord game server. Barnabus does **not** chat and does **not** generate images. He runs the mod log, keeps per-user records, watches for spam, floods and unsafe content, hands out XP ranks, reposts highlights, keeps stats channels current, and answers game questions strictly from a FAQ file and a lore file.

The language model (via OpenRouter) is used only for grounded tasks: `/ask`, `/lore`, Translate, the delayed forum answers and the daily digest. `LLMEnabled: false` turns all of those off and leaves every moderation feature running.

## Features

* **Mod log** — message edits, deletes, bulk deletes, bans and unbans, as embeds in a log channel. Toggles: `ModLogEdits`, `ModLogDeletes`, `ModLogIgnoredChannels`, `ModLogEditMinChange` (skip trivial edits).
* **User records** — SQLite history per member: spam/flood flags, honeypot trips, safety flags, moderator notes. `!record`, `!note`, `!whois`, `!forget` (erase a member's data on request). Message excerpts are encrypted at rest with a key in `record.key` (generated on first run, keep it backed up) and blanked after `RecordExcerptDays` (default 30).
* **Safety monitor** — every server message (and the replies of any bot listed in `SafetyMonitorBotIDs`) runs through a filter for sexualised-minor content. A hit writes a record and alerts the mod channel; only terms with no innocent use page `@here`. **Nothing is deleted and nobody is punished automatically.** The filter is the same code as the chat filter in IsabellBot; keep the two in sync.
* **Spam & flood** — invite/scam links from new accounts, the same message across several channels.
* **Honeypot** — anyone posting in the trap channel is softbanned (or banned) and their recent messages wiped. Owner, admins and exempt roles are ignored.
* **XP & ranks** — 15–25 XP per message with a cooldown; rank roles are swapped on promotion; `!rank`, `!top`. Level N→N+1 costs 5N²+50N+100.
* **Highlights** — enough ⭐/❤️ reactions repost a message to the highlights channel. Images only by default; bots' posts count only when listed in `HighlightBotIDs`.
* **Stats channels** — member count and Steam players online, in channel names (never faster than every 10 minutes).
* **FAQ** — `/ask` answers privately from `game_faq.txt`; forum threads that nobody answered for 30 minutes get one answer, grounded in the file, capped per scan.
* **Lore** — `/lore` answers in character from `world_lore.txt`.
* **Wiki** — `/wiki` searches a MediaWiki, no model involved.
* **Translate** — right-click a message → Apps → Translate, into the user's Discord language, shown only to them.
* **Daily digest** — a summary of the day's activity DM'd to the owner; `!summary [Nh]` on demand.

Mod commands work in DMs or the mod channel: `!help`, `!summary`, `!activity`, `!whois`, `!flags`, `!search`, `!note`, `!record`, `!checkperms` (reports missing permissions per configured channel and rank roles above the bot); owner: `!reload`.

## Setup

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp Config.example.json Barnabus.json   # fill in DiscordToken, OpenAPIKey, IDs
venv/bin/python -m barnabus
```

Config is read from `$BOT_CONFIG`, else `Config.json`, else `Barnabus.json`. It hot-reloads on change, as do the lore and FAQ files.

### Discord application

1. Developer Portal → New Application → Bot. Copy the token into the config.
2. Privileged intents: **Message Content** (required for moderation). Discord may require the "Request Intents" form before it can be enabled; until it is approved, set `EnableMessageContentIntent: false` and the bot runs in a reduced mode (honeypot, XP, highlights, stats, ban/unban log, slash commands, DM commands). **Server Members** only if you set `EnableMembersIntent` (needed for `!whois` by name).
3. Invite with scopes `bot` + `applications.commands` and these permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions, Manage Messages, Manage Roles, Manage Channels, Ban Members, Kick Members, View Audit Log.
4. Put the bot's role **above** every rank role it hands out, and give it access to the mod, mod-log, honeypot, highlights, questions-forum and stats channels. The invite grants server-wide permissions, but a private channel that hides `@everyone` also hides the bot until its role gets an override there. Run `!checkperms` in the mod channel to see exactly what is missing; the same report is written to the log at startup.
5. `AppCommandGuildID` makes slash commands register instantly for that server; leave `0` for global (up to an hour).

## Files

| File | Purpose | Committed? |
|---|---|---|
| `Barnabus.json` | real config with token and key | no |
| `barnabus.db` | records, XP, highlights | no |
| `record.key` | encryption key for stored excerpts | no |
| `game_faq.txt` | `Q:`/`A:` blocks, one per paragraph | no |
| `world_lore.txt` | markdown lore, `##`/`###` sections | no |
| `app.log` | rotating daily log | no |

## Privacy

What the bot stores, what leaves the machine and for how long is in [Privacy.md](Privacy.md).

## Deploying

See [deploy/VPS.md](deploy/VPS.md) for a systemd install on a small Linux VPS. The bot needs only outbound network access.
