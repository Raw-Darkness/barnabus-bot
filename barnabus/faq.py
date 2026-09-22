"""FAQ answers grounded strictly in the FAQ file: /ask on demand, and delayed
answers in the questions forum when nobody else has replied."""
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord import app_commands

from . import core
from .llm import chat_async, llm_enabled, llm_unavailable, utility_model
from .lore import query_words, read_text

_CACHE: dict[str, Any] = {"path": None, "mtime": 0.0, "text": "", "entries": []}


def _load_faq() -> tuple[str, list[tuple[str, dict[str, float]]]]:
    path = core.config.get("FAQPath", "game_faq.txt")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "", []
    if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
        return _CACHE["text"], _CACHE["entries"]
    text = read_text(path)
    parsed, freq = [], {}
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block.lower().startswith("q:"):
            continue
        words = set(re.findall(r"[a-z]{3,}", block.lower()))
        parsed.append((block, words))
        for w in words:
            freq[w] = freq.get(w, 0) + 1
    entries = [(b, {w: 1.0 / freq[w] for w in words}) for b, words in parsed]
    _CACHE.update(path=path, mtime=mtime, text=text, entries=entries)
    logging.info("FAQ loaded: %d entries, ~%d tokens", len(entries), len(text) / 3.6)
    return text, entries


def retrieve_faq(question: str) -> str:
    """Whole file while it is small; best-matching entries once it grows past FAQRetrievalMinTokens."""
    text, entries = _load_faq()
    if not text:
        return ""
    if len(text) / 3.6 <= float(core.config.get("FAQRetrievalMinTokens", 8000)) or not entries:
        return text
    qwords = query_words(question.lower())
    scored = sorted(((sum(w for t, w in terms.items() if t in qwords), b) for b, terms in entries), key=lambda x: -x[0])
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return text
    budget, picked = float(core.config.get("FAQRetrievalMaxTokens", 2500)), []
    for _, block in scored:
        cost = len(block) / 3.6
        if cost > budget:
            break
        picked.append(block)
        budget -= cost
    return "\n\n".join(picked) if picked else text


async def faq_answer(question: str) -> str | None:
    faq = retrieve_faq(question)
    if not faq:
        return None
    persona = (core.config.get("Personality") or "").strip()
    system = (
        f"You are {core.bot_name()}, answering a player's question about the game. "
        "Answer ONLY with information from the FAQ below — never invent, never use outside knowledge. "
        "Be concrete and concise (under 150 words). Keep the character's voice but clarity beats persona. "
        "If the FAQ does not clearly answer the question, reply with exactly: NO_ANSWER\n\n"
        + (f"Character:\n{persona[:1200]}\n\n" if persona else "")
        + "FAQ:\n" + faq
    )
    try:
        reply = await chat_async([{"role": "system", "content": system},
                                  {"role": "user", "content": f"Player question:\n{question}"}],
                                 temperature=0.3, max_tokens=400, model=core.config.get("FAQModel") or utility_model())
    except Exception:
        logging.exception("FAQ: LLM call failed")
        return None
    reply = (reply or "").strip()
    return None if not reply or "NO_ANSWER" in reply else reply


_ask_buckets: dict[int, core.TokenBucket] = {}


def ask_bucket(uid: int) -> core.TokenBucket:
    return core.bucket(_ask_buckets, uid, 3, 1.0 / 20.0)


@core.tree.command(name="ask", description="Ask the game FAQ — the answer is shown only to you")
@app_commands.describe(question="Your question about the game")
async def ask_command(interaction: discord.Interaction, question: str):
    try:
        if not core.config.get("FAQEnabled", True):
            await interaction.response.send_message("The FAQ is currently disabled.", ephemeral=True)
            return
        if not llm_enabled():
            await llm_unavailable(interaction)
            return
        if not ask_bucket(interaction.user.id).consume():
            await interaction.response.send_message(core.text("MsgRateLimited", "One at a time. The forge doesn't rush."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        answer = await faq_answer(question.strip()[:500])
        if answer is None:
            forum = core.cfg_int("QuestionsForumID")
            hint = f" Ask in <#{forum}>; someone on the island will know." if forum else ""
            await interaction.followup.send(core.text("MsgFAQNoAnswer", "Not in my ledger.") + hint, ephemeral=True)
            return
        await interaction.followup.send(answer[:1900], ephemeral=True)
        logging.info("/ask: answered %r", question[:80])
    except Exception:
        logging.exception("/ask failed")


# ---- Forum scanner ----------------------------------------------------------
_evaluated: dict[int, float] = {}  # thread_id -> FAQ mtime when judged


async def scan_once() -> None:
    forum_id = core.cfg_int("QuestionsForumID")
    if not forum_id or not core.config.get("FAQEnabled", True) or not llm_enabled():
        return
    forum = core.bot.get_channel(forum_id)
    if forum is None or not hasattr(forum, "threads"):
        return
    try:
        faq_mtime = os.path.getmtime(core.config.get("FAQPath", "game_faq.txt"))
    except OSError:
        return
    delay = timedelta(minutes=int(core.config.get("FAQAnswerDelayMin", 30)))
    max_age = timedelta(hours=int(core.config.get("FAQMaxThreadAgeHours", 24)))
    max_answers = int(core.config.get("FAQMaxAnswersPerScan", 3))
    now, answered = datetime.now(timezone.utc), 0
    for thread in list(forum.threads):
        if answered >= max_answers:
            break
        created = getattr(thread, "created_at", None)
        if created is None or not (delay <= now - created <= max_age) or _evaluated.get(thread.id) == faq_mtime:
            continue
        try:
            msgs = [m async for m in thread.history(limit=50, oldest_first=True)]
        except Exception:
            continue
        if any(m.author.id == core.bot.user.id for m in msgs):
            continue
        if any(not m.author.bot and m.author.id != thread.owner_id for m in msgs):
            continue  # a human already replied
        starter = next((m for m in msgs if m.author.id == thread.owner_id and (m.content or "").strip()), None)
        question = (thread.name or "") + (f"\n{starter.content[:1500]}" if starter else "")
        _evaluated[thread.id] = faq_mtime
        reply = await faq_answer(question)
        if reply is None:
            continue
        footer = core.text("FAQFooter", "\n-# I answer from the FAQ when a question has sat a while. Others on the island may know more.")
        await core.safe_send(thread, reply + footer)
        logging.info("FAQ: answered thread %r", question[:80])
        answered += 1
