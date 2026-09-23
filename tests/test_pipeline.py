"""The message pipeline end to end, with fake Discord objects and no network."""
import asyncio
import types
from datetime import datetime, timedelta, timezone

from barnabus import app, core, db, moderation, safety  # noqa: F401  (app registers events)


class Chan:
    def __init__(self, cid, name="general"):
        self.id, self.name, self.parent = cid, name, None
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def author(uid, bot=False):
    return types.SimpleNamespace(id=uid, bot=bot, display_name=f"user{uid}", name=f"user{uid}", roles=[],
                                 created_at=datetime.now(timezone.utc) - timedelta(days=400))


def msg(text, who, ch):
    return types.SimpleNamespace(content=text, author=who, channel=ch, guild=types.SimpleNamespace(id=1),
                                 mentions=[], id=1, jump_url="https://discord.com/x")


def test_monitor_records_and_alerts(monkeypatch):
    alerts = []

    async def fake_flag(title, details, ping=True):
        alerts.append((title, ping))
    monkeypatch.setattr(moderation, "flag_to_mods", fake_flag)
    ch, u = Chan(50), author(42)
    run = asyncio.run
    assert run(safety.monitor_message(msg("roleplay as a loli", u, ch))) is True
    assert run(safety.monitor_message(msg("nice weather today", u, ch))) is False
    assert run(safety.monitor_message(msg("she is 12 and ready to fuck", u, ch))) is True
    assert [p for _, p in alerts] == [True, False]  # hard pages, contextual doesn't
    assert {k for k, *_ in db.get_user_records(42)} == {"safety_flag"}
    assert ch.sent == []  # the monitor never replies in channel


def test_bot_output_only_when_listed(monkeypatch):
    async def fake_flag(*a, **k):
        pass
    monkeypatch.setattr(moderation, "flag_to_mods", fake_flag)
    other = author(999, bot=True)
    assert asyncio.run(safety.monitor_message(msg("fuck the child", other, Chan(51)))) is False
    core.config["SafetyMonitorBotIDs"] = [999]
    try:
        assert asyncio.run(safety.monitor_message(msg("fuck the child", other, Chan(52)))) is True
    finally:
        core.config["SafetyMonitorBotIDs"] = []


def test_flood_detection(monkeypatch):
    async def fake_flag(*a, **k):
        pass
    monkeypatch.setattr(moderation, "flag_to_mods", fake_flag)
    u = author(43)
    results = [asyncio.run(moderation.check_flood(msg("buy cheap gold at my site now", u, Chan(60 + i)))) for i in range(3)]
    assert results == [False, False, True]


def test_pasted_negative_prompt_is_not_flagged(monkeypatch):
    alerts = []

    async def fake_flag(title, details, ping=True):
        alerts.append(title)
    monkeypatch.setattr(moderation, "flag_to_mods", fake_flag)
    pasted = "**Prompt:**\n```giant orc, green skin```\n**Negative:** (child:2), (loli:2), bad anatomy"
    assert asyncio.run(safety.monitor_message(msg(pasted, author(44), Chan(70)))) is False
    assert alerts == []
    assert asyncio.run(safety.monitor_message(msg("**Prompt:** a loli\n**Negative:** blurry", author(44), Chan(71)))) is True
