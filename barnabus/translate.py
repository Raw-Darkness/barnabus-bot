"""Right-click a message → Apps → Translate: into the user's own Discord language, shown only to them."""
import logging

import discord

from . import core
from .llm import chat_async, llm_enabled, llm_unavailable, utility_model

_LOCALE_LANG = {
    "en-US": "English", "en-GB": "English", "de": "German", "sv-SE": "Swedish", "fr": "French",
    "es-ES": "Spanish", "es-419": "Spanish (Latin America)", "pt-BR": "Portuguese (Brazil)",
    "it": "Italian", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "ja": "Japanese", "ko": "Korean", "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
    "cs": "Czech", "da": "Danish", "fi": "Finnish", "no": "Norwegian", "hu": "Hungarian", "ro": "Romanian",
    "el": "Greek", "bg": "Bulgarian", "hr": "Croatian", "lt": "Lithuanian", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "hi": "Hindi", "ar": "Arabic", "he": "Hebrew",
}
_buckets: dict[int, core.TokenBucket] = {}


def _language(locale) -> str:
    code = str(locale)
    return _LOCALE_LANG.get(code) or _LOCALE_LANG.get(code.split("-")[0]) or f"the language for locale '{code}'"


@core.tree.context_menu(name="Translate")
async def translate_message(interaction: discord.Interaction, message: discord.Message):
    try:
        text = (message.content or "").strip()
        if not text:
            await interaction.response.send_message("Nothing to translate in that message.", ephemeral=True)
            return
        if not llm_enabled():
            await llm_unavailable(interaction)
            return
        if not core.bucket(_buckets, interaction.user.id, 5, 5.0 / 60.0).consume():
            await interaction.response.send_message(core.text("MsgRateLimited", "One at a time. The forge doesn't rush."), ephemeral=True)
            return
        language = _language(interaction.locale)
        await interaction.response.defer(ephemeral=True, thinking=True)
        system = (f"Translate the user's Discord message into {language}. Output ONLY the translation — no commentary. "
                  "Preserve meaning, tone, slang, emoji, @mentions, links, and Discord formatting. "
                  f"If the message is already in {language}, reply with a one-line note in {language} saying so.")
        try:
            out = await chat_async([{"role": "system", "content": system}, {"role": "user", "content": text[:1800]}],
                                   temperature=0.2, max_tokens=700, model=core.config.get("TranslateModel") or utility_model())
        except Exception:
            logging.exception("Translate: LLM call failed")
            out = None
        out = (out or "").strip()
        if not out:
            await interaction.followup.send("Couldn't translate that one. Try again in a moment.", ephemeral=True)
            return
        await interaction.followup.send(f"**{language}:**\n{out[:1850]}", ephemeral=True)
    except Exception:
        logging.exception("Translate failed")
