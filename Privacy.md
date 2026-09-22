# Privacy

Barnabus is a moderation bot. This is what it stores and where it sends data.

**Stored locally, in the bot's own database:** per-user moderation records (spam/flood flags, honeypot trips, safety-filter flags with a short text excerpt, moderator notes), XP totals and message counts, and the IDs of messages already posted as highlights. Records are kept until a moderator removes them.

**Sent to a third-party AI service (OpenRouter):** the text of a message when a user asks for a translation of it; the text of a question asked with `/ask` or `/lore` or posted in the questions forum; and, for the daily digest, recent public channel messages with display names. Nothing is sent for moderation decisions; the safety filter runs locally. No data is used to train models.

**Logged:** a rotating local log with moderation events and errors, kept for 7 days.

**Not collected:** direct messages are not stored, voice is not used, and no data is shared with anyone outside the server's moderation team.
