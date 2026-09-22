# Barnabus Privacy Policy

*Last updated: 2026-09-22*

Barnabus is a moderation and community bot operated by the Wicked Island Discord server's staff. This document explains what data the bot processes, why, where it goes and for how long. It applies to every server the bot is installed in.

## 1. What the bot collects

**Moderation records.** When the bot flags a message (spam or scam link, cross-channel flooding, honeypot trip, or a safety-filter match) it stores the member's Discord user ID, the kind of flag, a text excerpt of up to 500 characters, and the time. Moderators can also attach free-text notes to a member's record. These records exist so that staff can see a member's history before making a decision.

**Activity counters.** For the XP and rank system the bot stores each member's user ID, current display name, XP total, level, message count and the time of the last award. No message content is stored for this purpose.

**Highlights.** The IDs of messages that have already been reposted to the highlights channel, so the same message is never posted twice.

**Message cache.** Like any Discord bot that logs edits and deletions, the bot keeps a short-lived in-memory cache of recent messages so it can show what was changed or removed. The cache is not written to disk and is lost when the bot restarts.

**Logs.** A local log file records moderation events, errors and command usage. It rotates daily and is deleted after 7 days.

## 2. What the bot does not collect

* Direct messages to the bot are not stored. The bot only reads DMs to process moderator commands.
* Voice is never used.
* The bot does not store message content for ordinary messages. It reads them in real time to check for spam, floods and unsafe content and then discards them.
* The bot never collects email addresses, IP addresses or any information outside what Discord exposes to bots in the server.

## 3. Third-party processing

Some features send text to OpenRouter, an AI model gateway, to produce a response. This happens only in these cases:

* **Translate** — the text of the one message a user asked to translate.
* **/ask and /lore** — the question the user typed, together with the server's FAQ or lore document.
* **Forum answers** — the title and first post of a question thread that has gone unanswered.
* **Daily digest** — recent public channel messages with display names, so the bot can summarise the day's activity for the server owner. This feature can be disabled by the operator.

Moderation decisions never involve a third party. The safety filter runs locally on the bot's own machine.

OpenRouter's handling of this data is governed by its own privacy policy. The operator configures requests so that the data is not used to train models. The operator can disable every third-party call with a single configuration switch (`LLMEnabled`), after which the bot runs moderation features only.

## 4. Automated decisions

The bot takes exactly one automated action: a member who posts in the designated honeypot channel is removed from the server and their recent messages are deleted. The channel is clearly marked and hidden from normal navigation; server owners, administrators and exempt roles are never affected.

Every other flag, including safety-filter matches, results only in a record and a notification to the moderation team. **A human moderator decides what happens next.** Contextual matches are labelled as possible misunderstandings when they reach moderators.

## 5. Retention

| Data | Kept for |
|---|---|
| Moderation records and notes | until a moderator deletes them or the member's data is removed on request |
| XP and rank data | while the member is in the server; removable on request |
| Highlight message IDs | indefinitely (IDs only, no content) |
| Message cache | in memory only, lost on restart |
| Log files | 7 days |

## 6. Where data is stored

All persistent data lives in a single database file on the server that runs the bot, accessible only to the operator. It is not shared with other servers, other bots or any third party other than as described in section 3.

## 7. Your rights

You can ask the server's moderation team to show you what the bot holds about you, or to delete it. Deletion removes your moderation records, notes and XP data. Contact the server staff through the server's moderation channels or by DM to a moderator.

## 8. Changes

This policy changes when the bot's features change. The date at the top reflects the last revision.
