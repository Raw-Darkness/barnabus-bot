"""Offline checks for the encrypted local bug review client."""

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import time

import pytest


MODULE = Path(__file__).parents[1] / "tools" / "bug_review.py"
spec = importlib.util.spec_from_file_location("bug_review", MODULE)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


@pytest.fixture
def setup(tmp_path):
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    client.review_crypto.generate_keypair(private, public)
    path = tmp_path / "config.json"
    data = {"host": "review@example.test",
            "remote_spool": "/opt/barnabus/app/bug-review",
            "local_dir": str(tmp_path / "local"),
            "private_key": str(private), "public_key": str(public),
            "encrypted_drive_confirmed": True}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def sample(now=None):
    now = time.time() if now is None else now
    expiry = now + 20 * 86400
    return {
        "schema": 2, "generated_at": "2026-10-08T00:00:00Z",
        "expires_at": expiry, "content_available": True,
        "threads": [
            {"id": "2", "guild_id": "g", "forum_id": "f", "source": "public",
             "title": "Crash at launch", "url": "https://example.test/2",
             "revision": "b" * 64, "content_available": True,
             "expires_at": expiry, "messages": [{"id": "m2", "content": "on Linux"}],
             "history_truncated": False},
            {"id": "1", "guild_id": "g", "forum_id": "f", "source": "internal",
             "title": "Crash at launch", "url": "https://example.test/1",
             "revision": "a" * 64, "content_available": True,
             "expires_at": expiry, "messages": [{"id": "m1", "content": "on Windows"}],
             "history_truncated": False},
        ],
        "metadata_threads": [
            {"id": "3", "guild_id": "g", "forum_id": "f", "source": "public",
             "title": "Old controller issue"},
        ],
    }


def encrypted_local(setup, value=None):
    path, data = setup
    value = sample() if value is None else value
    local = Path(data["local_dir"])
    local.mkdir(exist_ok=True)
    envelope = client.review_crypto.seal(value, Path(data["public_key"]), value["expires_at"])
    (local / client.SNAPSHOT).write_text(json.dumps(envelope), encoding="utf-8")
    return local


def test_config_rejects_unsafe_hosts_paths_and_key_location(setup, tmp_path):
    path, data = setup
    for host in ("", "-oProxyCommand=evil", "x;evil", "x:/tmp", "a b"):
        path.write_text(json.dumps({**data, "host": host}), encoding="utf-8")
        with pytest.raises(client.ReviewError):
            client.config_from(path)
    path.write_text(json.dumps({**data, "remote_spool": "/tmp/../bad"}), encoding="utf-8")
    with pytest.raises(client.ReviewError):
        client.config_from(path)
    path.write_text(json.dumps({**data, "local_dir": str(Path.home())}), encoding="utf-8")
    with pytest.raises(client.ReviewError):
        client.config_from(path)
    path.write_text(json.dumps({**data, "private_key": str(tmp_path / "local" / "key.pem")}), encoding="utf-8")
    with pytest.raises(client.ReviewError):
        client.config_from(path)


def test_list_search_show_decrypt_in_memory_and_support_metadata(setup, monkeypatch, capsys):
    path, data = setup
    local = encrypted_local(setup)
    monkeypatch.setattr(client.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected transfer"))
    assert client.main(["--config", str(path), "list"]) == 0
    output = capsys.readouterr().out
    assert output.index("[internal]") < output.index("[public]")
    assert "[public] 3" in output
    assert client.main(["--config", str(path), "search", "Windows", "--duplicates"]) == 0
    output = capsys.readouterr().out
    assert "[internal] 1" in output and "[public] 2" not in output
    assert "possible duplicate: 2" in output
    assert client.main(["--config", str(path), "show", "1"]) == 0
    assert '"content": "on Windows"' in capsys.readouterr().out
    assert not (local / "reports.json").exists()
    assert "on Windows" not in (local / client.SNAPSHOT).read_text(encoding="utf-8")


def test_list_warns_about_incomplete_exports(capsys):
    value = sample()
    value["content_available"] = False
    value["threads"][0]["history_truncated"] = True
    value["forums"] = [{"id": "f", "truncated": True}]
    value["errors"] = [{"forum_id": "f", "error": "read_failed"}]
    client.list_reports(value, "no match")
    captured = capsys.readouterr()
    assert captured.out.strip() == "0 report(s)"
    assert "message content is unavailable" in captured.err
    assert "histories are truncated" in captured.err
    assert "more threads than exported" in captured.err


def test_search_includes_embed_text_without_url(setup, capsys):
    path, _ = setup
    value = sample()
    value["threads"][0]["messages"][0] = {
        "content": "", "embeds": [{"title": "Decky report", "description": "Fatal shader crash",
        "fields": [{"name": "Platform", "value": "Steam Deck"}],
        "footer": {"text": "Trace captured"}, "url": "https://example.test/needle-url"}]}
    encrypted_local(setup, value)
    assert client.main(["--config", str(path), "search", "shader"]) == 0
    assert "[public] 2" in capsys.readouterr().out
    assert client.main(["--config", str(path), "search", "needle-url"]) == 0
    assert capsys.readouterr().out.strip() == "0 report(s)"


def test_draft_submit_uses_managed_ledger_encrypted_receipt_and_ssh_stdin(setup, monkeypatch, capsys):
    path, data = setup
    local = encrypted_local(setup)
    assert client.main(["--config", str(path), "draft", "1"]) == 0
    draft = Path(capsys.readouterr().out.strip())
    assert draft.parent == local / "drafts"
    draft.write_text("A useful suggestion", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        assert args[0] == "ssh"
        assert "BatchMode=yes" in args and "ConnectTimeout=15" in args
        assert args[-1] == "python3 /opt/barnabus/app/tools/bug_review_send.py /opt/barnabus/app/bug-review/bridge.sock"
        request = json.loads(kwargs["input"])
        assert request["markdown"] == "A useful suggestion"
        assert request["expires_at"] <= sample()["expires_at"] + 1
        return subprocess.CompletedProcess(args, 0, stdout='{"status":"posted"}')

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.main(["--config", str(path), "submit", "1", str(draft)]) == 0
    output = capsys.readouterr().out.splitlines()
    request_id = output[0]
    assert re.fullmatch("[0-9a-f]{32}", request_id)
    assert json.loads(output[1])["status"] == "posted"
    assert len(calls) == 1
    encrypted = (local / "submissions" / f"{request_id}.enc.json").read_text(encoding="utf-8")
    assert "A useful suggestion" not in encrypted
    assert not (local / "submissions" / f"{request_id}.json").exists()
    with pytest.raises(client.ReviewError, match="local_dir/drafts"):
        client.build_request(sample(), "1", path, local, client._ledger(local))


def test_submit_rejects_public_missing_content_and_expired_source(setup):
    path, data = setup
    local = encrypted_local(setup)
    value = sample()
    with pytest.raises(client.ReviewError, match="internal"):
        client.create_draft(client.config_from(path), value, "2")
    value["content_available"] = False
    with pytest.raises(client.ReviewError, match="no message content"):
        client.create_draft(client.config_from(path), value, "1")
    value = sample()
    value["threads"][1]["expires_at"] = time.time() - 1
    with pytest.raises(client.ReviewError, match="expired"):
        client.create_draft(client.config_from(path), value, "1")
    assert not list((local / "drafts").glob("*.md")) if (local / "drafts").exists() else True


def test_cleanup_purges_expired_and_legacy_even_when_drive_disabled(setup, capsys):
    path, data = setup
    local = encrypted_local(setup)
    (local / "reports.json").write_text('{"secret":"legacy"}', encoding="utf-8")
    config = client.config_from(path)
    draft = client.create_draft(config, sample(), "1")
    ledger = client._ledger(local)
    ledger["drafts"][draft.name]["expires_at"] = time.time() - 1
    client._save_ledger(local, ledger)
    data["encrypted_drive_confirmed"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    assert client.main(["--config", str(path), "cleanup"]) == 0
    assert capsys.readouterr().out == ""
    assert not draft.exists() and not (local / "reports.json").exists()
    assert client.main(["--config", str(path), "list"]) == 1
    assert "encrypted_drive_confirmed" in capsys.readouterr().err


def test_invalid_encrypted_snapshot_is_purged(setup, capsys):
    path, data = setup
    local = Path(data["local_dir"])
    local.mkdir()
    report = local / client.SNAPSHOT
    report.write_text('{"schema":1,"expires_at":9999999999}', encoding="utf-8")
    assert client.main(["--config", str(path), "list"]) == 1
    assert not report.exists()
    assert "run fetch first" in capsys.readouterr().err


def test_fetch_and_status_download_without_plaintext_persistence(setup, monkeypatch, capsys):
    path, data = setup
    value = sample()
    envelope = client.review_crypto.seal(value, Path(data["public_key"]), value["expires_at"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "reports.enc.json" in args[-2]:
            Path(args[-1]).write_text(json.dumps(envelope), encoding="utf-8")
        else:
            Path(args[-1]).write_text('{"status":"posted"}', encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.main(["--config", str(path), "fetch"]) == 0
    local = Path(data["local_dir"])
    assert (local / client.SNAPSHOT).exists()
    assert not (local / "reports.json").exists()
    capsys.readouterr()
    receipt_id = "a" * 32
    assert client.main(["--config", str(path), "status", receipt_id]) == 0
    assert '"status":"posted"' in capsys.readouterr().out
    assert not (local / "receipts" / f"{receipt_id}.json").exists()
    assert len(calls) == 2 and all(call[0] == "scp" for call in calls)
    assert client.main(["--config", str(path), "status", "../bad"]) == 1
    assert len(calls) == 2


def test_ambiguous_send_keeps_encrypted_request_and_prints_id(setup, monkeypatch, capsys):
    path, data = setup
    local = encrypted_local(setup)
    config = client.config_from(path)
    draft = client.create_draft(config, sample(), "1")
    draft.write_text("Review", encoding="utf-8")

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.main(["--config", str(path), "submit", "1", str(draft)]) == 1
    captured = capsys.readouterr()
    request_id = captured.out.strip()
    assert re.fullmatch("[0-9a-f]{32}", request_id)
    saved = (local / "submissions" / f"{request_id}.enc.json")
    assert saved.exists() and "Review" not in saved.read_text(encoding="utf-8")
    assert "ssh transfer timed out" in captured.err
    assert f"check status {request_id} before retrying" in captured.err



def test_submit_rejected_receipt_is_visible(setup, monkeypatch, capsys):
    path, data = setup
    encrypted_local(setup)
    config = client.config_from(path)
    draft = client.create_draft(config, sample(), "1")
    draft.write_text("Review", encoding="utf-8")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout='{"status":"rejected","reason":"Posting disabled"}')

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.main(["--config", str(path), "submit", "1", str(draft)]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert re.fullmatch("[0-9a-f]{32}", lines[0])
    assert json.loads(lines[1])["reason"] == "Posting disabled"
    assert "Submission rejected" in captured.err


def test_copying_draft_does_not_register_new_source(setup):
    path, data = setup
    local = encrypted_local(setup)
    config = client.config_from(path)
    draft = client.create_draft(config, sample(), "1")
    copied = draft.with_name("copied-" + draft.name)
    copied.write_bytes(draft.read_bytes())
    client.cleanup(config)
    assert not copied.exists()
    assert draft.exists()


def test_expiry_margin_removes_snapshot_before_deadline(setup):
    path, data = setup
    now = time.time()
    value = sample(now)
    value["expires_at"] = now + 45
    for thread in value["threads"]:
        thread["expires_at"] = now + 45
    local = encrypted_local(setup, value)
    client.cleanup(client.config_from(path), now=now)
    assert not (local / client.SNAPSHOT).exists()


def test_expired_draft_is_removed_before_submit(setup, monkeypatch, capsys):
    path, data = setup
    local = encrypted_local(setup)
    config = client.config_from(path)
    draft = client.create_draft(config, sample(), "1")
    ledger = client._ledger(local)
    ledger["drafts"][draft.name]["expires_at"] = time.time() - 1
    client._save_ledger(local, ledger)
    monkeypatch.setattr(client.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected transfer"))
    assert client.main(["--config", str(path), "submit", "1", str(draft)]) == 1
    assert not draft.exists()
    assert not (local / "submissions").exists()
    assert "does not exist" in capsys.readouterr().err


def test_cleanup_purges_near_expiry_encrypted_submission(setup):
    path, data = setup
    local = Path(data["local_dir"])
    submissions = local / "submissions"
    submissions.mkdir(parents=True)
    expiry = time.time() + 45
    envelope = client.review_crypto.seal({"markdown": "private"}, Path(data["public_key"]), expiry)
    saved = submissions / ("a" * 32 + ".enc.json")
    saved.write_text(json.dumps(envelope), encoding="utf-8")
    client.cleanup(client.config_from(path))
    assert not saved.exists()


def test_missing_private_key_preserves_valid_encrypted_snapshot(setup, capsys):
    path, data = setup
    local = encrypted_local(setup)
    data["private_key"] = str(local.parent / "missing.pem")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert client.main(["--config", str(path), "list"]) == 1
    assert (local / client.SNAPSHOT).exists()
    assert "private_key" in capsys.readouterr().err


def test_selected_thread_can_outlive_global_snapshot_deadline(setup):
    path, data = setup
    now = time.time()
    value = sample(now)
    value["expires_at"] = now + 86400
    value["threads"][0]["expires_at"] = now + 86400
    value["threads"][1]["expires_at"] = now + 5 * 86400
    encrypted_local(setup, value)
    config = client.config_from(path)
    draft = client.create_draft(config, value, "1", now=now)
    entry = client._ledger(config["local_dir"])["drafts"][draft.name]
    assert entry["expires_at"] == value["expires_at"]
    draft.write_text("Review", encoding="utf-8")
    request = client.build_request(value, "1", draft, config["local_dir"],
                                   client._ledger(config["local_dir"]), now=now)
    assert request["expires_at"] == value["expires_at"]


def test_cleanup_works_without_valid_transport_or_keys(setup, capsys):
    path, data = setup
    local = encrypted_local(setup)
    config = client.config_from(path)
    draft = client.create_draft(config, sample(), "1")
    ledger = client._ledger(local)
    ledger["drafts"][draft.name]["expires_at"] = time.time() - 1
    client._save_ledger(local, ledger)
    (local / "reports.json").write_text("legacy private text", encoding="utf-8")
    now = time.time()
    value = sample(now)
    value["expires_at"] = now + 45
    envelope = client.review_crypto.seal(value, Path(data["public_key"]), value["expires_at"])
    (local / client.SNAPSHOT).write_text(json.dumps(envelope), encoding="utf-8")
    path.write_text(json.dumps({"host": "bad;host", "remote_spool": "/tmp/../bad",
                                "local_dir": str(local), "encrypted_drive_confirmed": False}),
                    encoding="utf-8")
    assert client.main(["--config", str(path), "cleanup"]) == 0
    assert capsys.readouterr().out == ""
    assert not draft.exists()
    assert not (local / "reports.json").exists()
    assert not (local / client.SNAPSHOT).exists()



def test_invalid_host_still_runs_cleanup_before_other_command(setup, capsys):
    path, data = setup
    local = Path(data["local_dir"])
    local.mkdir()
    legacy = local / "reports.json"
    legacy.write_text("legacy private text", encoding="utf-8")
    path.write_text(json.dumps({"host": "bad;host", "local_dir": str(local)}), encoding="utf-8")
    assert client.main(["--config", str(path), "list"]) == 1
    assert not legacy.exists()
    assert "hostname" in capsys.readouterr().err

def test_transfer_uses_utf8_for_nonwestern_stdin(monkeypatch):
    def fake_run(args, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["input"] == "snowman: \u2603"
        return subprocess.CompletedProcess(args, 0, stdout='{"status":"posted"}')

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    result = client.transfer(["ssh", "safe-host", "fixed-helper"], input_text="snowman: \u2603")
    assert json.loads(result.stdout)["status"] == "posted"


def test_transfer_timeout_has_no_command_or_config_output(monkeypatch):
    def time_out(args, **kwargs):
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr(client.subprocess, "run", time_out)
    with pytest.raises(client.ReviewError, match="scp transfer timed out") as error:
        client.transfer(["scp", "private-host:private-path"])
    assert "private-host" not in str(error.value)
