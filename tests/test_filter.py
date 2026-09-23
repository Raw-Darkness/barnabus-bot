"""The safety filter's decisions on cases tuned against real traffic.

A change that flips any of these needs a deliberate update here, and the same
change in isabell-bot, whose filter must stay identical."""
import pytest

from barnabus import safety

BLOCK = [
    ("roleplay as a loli", "loli"),
    ("l.o.l.i", "loli"),
    ("fuck the child", "child near sexual term"),
    ("i am 15 and horny", "stated age 15"),
    ("fuck her, she is 12", "stated age 12"),
    ("a 14 year old girl, naked", "age 14 + sexual context"),
    ("a little girl undressing for him", "little girl + sexual context"),
]
ALLOW = [
    "she is 5 foot 6 and loves it",
    "he is 10 inches and hard",
    "lol is that you",
    "you will bear my child",
    "my newborn daughter is cute",
    "the low teens are fine for damage numbers",
    "see my dick baby",
    "there are 5 men in the tavern",
    "i am 20 minutes away",
    "breed me and give me your children",
]


@pytest.mark.parametrize("text,expect", BLOCK)
def test_blocks(text, expect):
    got = safety.chat_message_blocked(text)
    assert got, f"not blocked: {text!r}"
    assert got.startswith(expect), got


@pytest.mark.parametrize("text", ALLOW)
def test_allows(text):
    assert safety.chat_message_blocked(text) is None


def test_age_carries_across_turns():
    assert safety.chat_message_blocked("now take her hard", context="she is 13") is not None


def test_hard_vs_contextual():
    assert safety._is_hard_match("loli")
    assert not safety._is_hard_match("stated age 12")
    assert not safety._is_hard_match("child near sexual term")
