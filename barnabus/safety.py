"""Safety filter, run as a passive monitor over server traffic.

Barnabus never replies to or punishes a flagged message. He writes a record,
alerts the moderators (paging them only for terms with no innocent use) and
leaves the decision to a human. The detection code is identical to the chat
filter in IsabellBot so both bots agree on what crosses the line; keep them in
sync when tuning.
"""
import logging
import re
import time
import unicodedata
from collections import deque

import discord

from . import core
from .db import add_user_record

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
                       "@": "a", "$": "s", "!": "i"})

_AGE_NUM_RE = re.compile(
    r"\b(\d{1,2})\s*(?:years?|yrs?)\s*old\b|\b(\d{1,2})\s*y\.?o\.?\b|\baged?\s*[:=]?\s*(\d{1,2})\b"
)


def _obfuscated(term: str, text: str) -> bool:
    """True if `term` appears deliberately broken up, e.g. "l.o.l.i" or "l o l i".

    Collapsing all whitespace instead would make "lol is" match "loli", which it
    did against real traffic — "lol" is far too common to treat that way.
    """
    punct = r"[._\-*+~|]+".join(re.escape(c) for c in term)
    spaced = r"\s+".join(re.escape(c) for c in term)
    return bool(re.search(rf"\b{punct}\b", text) or re.search(rf"\b{spaced}\b", text))


def _normalize_for_filter(text: str) -> tuple[str, str, str]:
    """Return (leet-folded, de-punctuated, digit-preserving) forms of the text.

    Leet folding maps digits onto letters so "l0li" is caught, which also
    destroys real numbers — so ages are matched against the untouched form.
    """
    base = unicodedata.normalize("NFKD", (text or "").lower())
    base = "".join(c for c in base if not unicodedata.combining(c))
    folded = re.sub(r"[^a-z0-9]+", " ", base.translate(_LEET)).strip()
    plain = re.sub(r"[^a-z0-9]+", " ", base).strip()
    return folded, folded.replace(" ", ""), plain


# ---- Chat safety -----------------------------------------------------------
# Images can refuse on any age word, because no legitimate prompt needs one.
# Chat cannot: this game's roleplay is about breeding and offspring, so "child",
# "children" and "baby" occur constantly and innocently. So chat is tiered:
#   1. terms with no innocent use               -> always refuse
#   2. words describing a minor as a person     -> refuse when the message is sexual
#   3. a stated age under 18                    -> refuse when the message is sexual
#   4. offspring words (child/kid/baby)         -> refuse only when a hard sexual
#      term sits within a few words of them, which separates "you will bear my
#      child" from "fuck the child".
_CHAT_ALWAYS_BLOCK = frozenset({
    "loli", "lolis", "lolicon", "lolita", "shota", "shotacon", "toddlercon",
    "jailbait", "jail bait", "pedo", "pedophile", "paedophile", "pedophilia",
    "underage", "under age", "preteen", "pre teen", "prepubescent", "child porn",
    "childporn", "csam", "child sex", "sex with a child", "sex with children",
})
_MINOR_DESCRIPTORS = frozenset({
    "young girl", "young boy", "little girl", "little boy", "small girl", "small boy",
    "schoolgirl", "school girl", "schoolboy", "school boy", "teen", "teens",
    "teenage", "teenager", "adolescent", "toddler",
    "grade school", "elementary school", "kindergarten", "middle school",
    "youngster", "minor girl", "minor boy",
})
# Offspring words: this community's roleplay is about breeding, so these are
# innocent unless a hard sexual term is right next to them — and they are only
# looked for in the current message, never carried over from earlier turns.
_AMBIGUOUS_OFFSPRING = ("child", "children", "kid", "kids", "baby", "babies", "newborn", "infant")
_SEXUAL_RE = re.compile(
    r"\b(fuck\w*|cock|dick|pussy|cunt|cum\w*|semen|breed\w*|naked|nude|sex|sexual|horny|"
    r"slut\w*|whore|virgin|penetrat\w*|rape|raping|impregnat\w*|tits|breasts|nipples|"
    r"moan\w*|orgasm\w*|aroused|erect\w*|thrust\w*|mount\w*|suck\w*|lick\w*|anal|oral|"
    r"blowjob|creampie|ravish\w*|deflower\w*|molest\w*|seduc\w*|undress\w*|strip\w*|"
    r"grope\w*|fondl\w*|caress\w*|bondage|submissive|dominate|lust\w*|arousal)\b"
    # Euphemisms only count with an object, so "take a look" stays innocent while
    # "take her hard" does not.
    r"|\b(take|takes|taking|took|claim\w*|bed|ride|rides|riding|use|using|touch\w*|"
    r"kiss\w*|have|had)\s+(you|her|him|me|them|his|their)\b"
    r"|\bmake love\b|\bhave my way\b|\bspread (her|your|his) legs\b", re.IGNORECASE)
# "you're 12", "i am 15", "she is 13" — an age with no "years old" attached.
_BARE_AGE_RE = re.compile(
    r"\b(?:you re|youre|you are|i m|im|i am|she is|shes|he is|hes)\s+(\d{1,2})\b"
    # Not an age when a measurement, a count or a second number follows:
    # "she is 5 foot", "he is 10 inches", "she is 5 6", "i am 20 minutes away".
    r"(?!\s*(?:\d|feet|foot|ft|inch|inches|in\b|cm|mm|m\b|meters?|metres?|kg|lbs?|pounds?|stone|"
    r"tall|long|wide|thick|big|percent|minutes?|hours?|days?|weeks?|months?|years? (?:in|into|of|since|ago|from)|"
    r"k\b|x\b|th\b|st\b|nd\b|rd\b|levels?|lvl|xp|points?|coins?|gold))")
# Deliberately narrower: these must sit *next to* an offspring word to trigger.
_HARD_SEXUAL = frozenset({
    "fuck", "fucks", "fucking", "fucked", "rape", "raped", "raping", "penetrate",
    "penetrated", "penetrating", "cock", "dick", "pussy", "cunt", "anal", "oral",
    "blowjob", "cum", "cumming", "suck", "sucking", "lick", "licking", "thrust",
    "thrusting", "deflower", "molest", "molesting", "horny",
})
# Only these descriptors are carried across turns. "schoolgirl"/"teen" are common
# costume and life-stage words in adult roleplay; carrying them forward flagged
# unrelated later messages and left moderators unable to find the trigger.
_MINOR_DESCRIPTORS_CARRIED = frozenset({
    "young girl", "young boy", "little girl", "little boy", "small girl", "small boy",
    "toddler", "kindergarten", "grade school", "elementary school", "minor girl", "minor boy",
})
_BABY_DETERMINERS = ("a", "the", "that", "this", "her", "his", "their", "our", "newborn", "little")


def _snippet(text: str, term: str, width: int = 28) -> str:
    m = re.search(rf"\b{re.escape(term)}\b", text, re.I)
    if not m:
        return ""
    s = text[max(0, m.start() - width): m.end() + width].replace("\n", " ")
    return f'"…{s}…"'


def chat_message_blocked(text: str, context: str = "") -> str | None:
    """The matched reason if this chat text must be refused, else None.

    `context` is the recent conversation. A minor established a few turns earlier
    ("roleplay as a 15 year old") is still a minor when the sexual turn arrives,
    so age indicators are searched across the exchange while the sexual trigger
    must be in the current message.
    """
    if not core.config.get("ChatFilterEnabled", True) or not text:
        return None
    norm, squashed, plain = _normalize_for_filter(text)
    if context:
        c_norm, _, c_plain = _normalize_for_filter(context)
        scope_norm, scope_plain = f"{c_norm} {norm}", f"{c_plain} {plain}"
    else:
        scope_norm, scope_plain = norm, plain
    extra = {str(t).lower().strip() for t in (core.config.get("ChatBlockExtraTerms") or []) if str(t).strip()}
    for term in set(_CHAT_ALWAYS_BLOCK) | extra:
        if (term in norm) if " " in term else re.search(rf"\b{re.escape(term)}\b", norm):
            return term
    folded_raw = unicodedata.normalize("NFKD", (text or "").lower()).translate(_LEET)
    for term in ("loli", "lolicon", "shota", "shotacon", "toddlercon", "jailbait", "pedo"):
        if _obfuscated(term, folded_raw):
            return term

    # "she is 13", "i am 15" — a person's stated age needs no sexual context to be
    # disqualifying here. ("the game is 4 years old" does not match: this pattern
    # requires a personal pronoun.)
    limit = int(core.config.get("ImageBlockAgeUnder", 18))
    for where, hay in (("", plain), ("[from an earlier message] ", c_plain if context else "")):
        for m in _BARE_AGE_RE.finditer(hay):
            if int(m.group(1)) < limit:
                return f"stated age {m.group(1)} {where}{_snippet(text if not where else context, m.group(1))}"

    if not _SEXUAL_RE.search(norm):
        return None

    for term in _MINOR_DESCRIPTORS:
        in_msg = (term in norm) if " " in term else re.search(rf"\b{re.escape(term)}\b", norm)
        # "the low teens" / "high teens" is a numeric range, not a person. Only
        # skip when EVERY occurrence is preceded by such a cue.
        if term == "teens" and in_msg:
            occ = list(re.finditer(r"\bteens\b", norm))
            if occ and all(re.search(r"\b(low|high|mid|upper|lower)\s*$", norm[:o.start()]) for o in occ):
                in_msg = None
        if in_msg:
            return f"{term} + sexual context {_snippet(text, term)}"
        if context and term in _MINOR_DESCRIPTORS_CARRIED:
            in_ctx = (term in c_norm) if " " in term else re.search(rf"\b{re.escape(term)}\b", c_norm)
            if in_ctx:
                return f"{term} + sexual context [from an earlier message: {_snippet(context, term)}]"
    for where, hay in (("", plain), ("[from an earlier message] ", c_plain if context else "")):
        for m in _AGE_NUM_RE.finditer(hay):
            num = next((g for g in m.groups() if g), None)
            if num is not None and int(num) < limit:
                return f"age {num} + sexual context {where}{_snippet(text if not where else context, m.group(0))}"

    words = norm.split()
    hard = [i for i, w in enumerate(words) if w in _HARD_SEXUAL]
    if hard:
        window = int(core.config.get("ChatOffspringProximity", 3))
        for i, w in enumerate(words):
            if w not in _AMBIGUOUS_OFFSPRING or not any(abs(i - j) <= window for j in hard):
                continue
            # "see my dick baby" is an endearment; "fuck the baby" is not.
            if w in ("baby", "babies") and (i == 0 or words[i - 1] not in _BABY_DETERMINERS):
                continue
            return f"{w} near sexual term {_snippet(text, w)}"
    return None


def _is_hard_match(matched: str) -> bool:
    """Tier-1 terms with no innocent use, vs. contextual matches that may be mistaken."""
    m = (matched or "").lower()
    return (" + " not in m and " near " not in m and not m.startswith(("age ", "stated age")))


# ---- Monitor -----------------------------------------------------------------
# Recent message texts per channel, so an age stated a few messages earlier still
# counts when the sexual turn arrives (same split-turn defence as the chat bot).
_recent: dict[int, deque[str]] = {}


def _context_for(channel_id: int) -> str:
    return " ".join(_recent.get(channel_id, ()))[-1500:]


def _remember(channel_id: int, text: str) -> None:
    d = _recent.get(channel_id)
    if d is None:
        d = _recent[channel_id] = deque(maxlen=6)
    d.append(text[:400])


def monitor_scope(message: discord.Message) -> bool:
    """Should this message be scanned at all?"""
    if not core.config.get("SafetyMonitorEnabled", True) or message.guild is None:
        return False
    if message.author.bot and message.author.id not in core.cfg_ids("SafetyMonitorBotIDs"):
        return False
    only = core.cfg_ids("SafetyMonitorChannels")
    if only and core.channel_key(message) not in only:
        return False
    return message.channel.id not in {core.mod_channel_id(), core.modlog_channel_id()}


async def monitor_message(message: discord.Message) -> bool:
    """Scan one message. Returns True if it was flagged. Never deletes, never replies."""
    text = (message.content or "").strip()
    if not text or not monitor_scope(message):
        return False
    ch = core.channel_key(message)
    matched = chat_message_blocked(text, _context_for(ch))
    _remember(ch, text)
    if not matched:
        return False
    hard = _is_hard_match(matched)
    author = message.author
    who = f"bot output from **{author.display_name}**" if author.bot else f"**{author.display_name}** ({author.id})"
    logging.warning("Safety monitor (%s) | %s | matched=%r | text=%r",
                    "hard" if hard else "contextual", who, matched, text[:200])
    kind = "safety_bot_output" if author.bot else "safety_flag"
    add_user_record(author.id, kind, f"matched '{matched}' in #{getattr(message.channel, 'name', message.channel.id)}: {text[:200]}")
    if core.config.get("SafetyMonitorAlertMods", True):
        from .moderation import flag_to_mods  # late import: moderation imports db too
        jump = message.jump_url
        await flag_to_mods(
            f"Safety monitor — {'HARD term' if hard else 'contextual match, please verify'}",
            f"User: {who}\nMatched: `{matched}`\nWhere: <#{message.channel.id}> · [jump]({jump})\nText: {text[:300]}",
            ping=hard,
        )
    return True
