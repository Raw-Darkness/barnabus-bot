import asyncio
import time

import pytest

from barnabus import core, db, llm, xp


def test_xp_curve():
    assert xp.level_progress(0) == (0, 0, 100)
    assert xp.level_progress(100) == (1, 0, 155)
    assert xp.xp_needed_for(10) == 5 * 100 + 500 + 100


def test_llm_switch_blocks_all_calls():
    core.config["LLMEnabled"] = False
    try:
        with pytest.raises(llm.LLMResponseError):
            asyncio.run(llm.chat_async([{"role": "user", "content": "x"}]))
    finally:
        core.config["LLMEnabled"] = True


def test_records_encrypted_expired_and_forgotten():
    db.add_user_record(1, "safety_flag", "secret excerpt")
    db.add_user_record(1, "note", "mod note")
    raw = [d for (d,) in db.get_db().execute("SELECT detail FROM user_records WHERE user_id = 1")]
    assert all(d.startswith("enc:") and "secret" not in d for d in raw)
    assert {d for _, d, _, _ in db.get_user_records(1)} == {"secret excerpt", "mod note"}
    db.get_db().execute("UPDATE user_records SET ts = ? WHERE kind = 'safety_flag'", (time.time() - 40 * 86400,))
    assert db.expire_excerpts() == 1
    assert {d for _, d, _, _ in db.get_user_records(1)} == {"", "mod note"}
    assert db.delete_user_data(1) == (2, 0)


def test_parse_user_ref():
    assert core.parse_user_ref("<@123>") == 123
    assert core.parse_user_ref("<@!456>") == 456
    assert core.parse_user_ref("789") == 789
    assert core.parse_user_ref("bob") is None


def test_secrets_from_environment(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "env-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    saved = dict(core.config)
    try:
        core.load_config()
        assert core.config["DiscordToken"] == "env-token"
        assert core.config["OpenAPIKey"] == "env-key"
    finally:
        core.config.clear()
        core.config.update(saved)


def test_main_starts_without_a_running_loop(monkeypatch):
    """main() runs before any event loop exists. Python 3.14 raises if startup code
    asks for a loop at that point, which crash-looped the first production start."""
    from barnabus import app
    started = []
    monkeypatch.setattr(core.bot, "run", lambda token: started.append(token))
    core.config["DiscordToken"] = "x"
    app.main()
    assert started == ["x"]


def test_signal_handlers_install_inside_the_loop():
    from barnabus import app

    async def inside():
        app._signals()
    asyncio.run(inside())
