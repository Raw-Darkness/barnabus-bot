"""Offline checks for the local bug review client."""

import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest


MODULE = Path(__file__).parents[1] / "tools" / "bug_review.py"
spec = importlib.util.spec_from_file_location("bug_review", MODULE)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "host": "review@example.test",
        "remote_spool": "/opt/barnabus/app/bug-review",
        "local_dir": str(tmp_path / "local"),
    }), encoding="utf-8")
    return path


def sample():
    return {
        "schema": 1, "generated_at": "2026-10-08T00:00:00Z",
        "content_available": True,
        "threads": [
            {"id": "2", "guild_id": "g", "forum_id": "f", "source": "public",
             "title": "Crash at launch", "url": "https://example.test/2",
             "revision": "b" * 64, "content_available": True,
             "messages": [{"id": "m2", "content": "on Linux"}],
             "history_truncated": False},
            {"id": "1", "guild_id": "g", "forum_id": "f", "source": "internal",
             "title": "Crash at launch", "url": "https://example.test/1",
             "revision": "a" * 64, "content_available": True,
             "messages": [{"id": "m1", "content": "on Windows"}],
             "history_truncated": False},
        ],
    }


def test_config_rejects_unsafe_hosts_and_paths(tmp_path):
    path = config(tmp_path)
    for host in ("", "-oProxyCommand=evil", "x;evil", "x:/tmp", "a b"):
        path.write_text(json.dumps({"host": host}), encoding="utf-8")
        with pytest.raises(client.ReviewError):
            client.config_from(path)
    path.write_text(json.dumps({"host": "safe.example", "remote_spool": "/tmp/../bad"}), encoding="utf-8")
    with pytest.raises(client.ReviewError):
        client.config_from(path)


def test_list_search_local_only_and_internal_first(tmp_path, monkeypatch, capsys):
    path = config(tmp_path)
    local = tmp_path / "local"
    local.mkdir()
    (local / "reports.json").write_text(json.dumps(sample()), encoding="utf-8")
    monkeypatch.setattr(client.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected transfer"))
    assert client.main(["--config", str(path), "list"]) == 0
    output = capsys.readouterr().out
    assert output.index("[internal]") < output.index("[public]")
    assert client.main(["--config", str(path), "search", "Windows", "--duplicates"]) == 0
    output = capsys.readouterr().out
    assert "[internal] 1" in output and "[public] 2" not in output
    assert "possible duplicate: 2" in output


def test_list_warns_about_incomplete_exports(tmp_path, capsys):
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
    assert "could not be exported" in captured.err


def test_search_includes_embed_text_without_url(tmp_path, monkeypatch, capsys):
    path = config(tmp_path)
    local = tmp_path / "local"
    local.mkdir()
    value = sample()
    value["threads"][0]["messages"][0]["content"] = ""
    value["threads"][0]["messages"][0]["embeds"] = [{
        "title": "Decky report", "description": "Fatal shader crash",
        "fields": [{"name": "Platform", "value": "Steam Deck"}],
        "footer": {"text": "Trace captured"},
        "url": "https://example.test/needle-url",
    }]
    (local / "reports.json").write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(client.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected transfer"))
    assert client.main(["--config", str(path), "search", "shader"]) == 0
    output = capsys.readouterr().out
    assert "[public] 2" in output
    assert client.main(["--config", str(path), "search", "needle-url"]) == 0
    assert capsys.readouterr().out.strip() == "0 report(s)"


def test_submit_checks_source_content_and_size(tmp_path):
    markdown = tmp_path / "review.md"
    markdown.write_text("Suggestion", encoding="utf-8")
    value = sample()
    with pytest.raises(client.ReviewError, match="internal"):
        client.build_request(value, "2", markdown)
    value["content_available"] = False
    with pytest.raises(client.ReviewError, match="no message content"):
        client.build_request(value, "1", markdown)
    value["content_available"] = True
    value["threads"][1]["content_available"] = False
    with pytest.raises(client.ReviewError, match="Selected report has no message content"):
        client.build_request(value, "1", markdown)
    value["threads"][1]["content_available"] = True
    markdown.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(client.ReviewError, match="64 KiB"):
        client.build_request(value, "1", markdown)
    markdown.write_bytes(b"\xff")
    with pytest.raises(client.ReviewError, match="Cannot read Markdown"):
        client.build_request(value, "1", markdown)


def test_submit_uploads_temp_then_renames(tmp_path, monkeypatch):
    path = config(tmp_path)
    markdown = tmp_path / "review.md"
    markdown.write_text("A useful suggestion", encoding="utf-8")
    request = client.build_request(sample(), "1", markdown)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "scp":
            uploaded = json.loads(Path(args[-2]).read_text(encoding="utf-8"))
            assert uploaded == request
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    client.submit(client.config_from(path), request)
    assert len(calls) == 2
    assert calls[0][0] == "scp" and calls[1][0] == "ssh"
    assert "BatchMode=yes" in calls[0] and "BatchMode=yes" in calls[1]
    assert "ConnectTimeout=15" in calls[0] and "ConnectTimeout=15" in calls[1]
    assert calls[0][-1].endswith(f"/outbox/{request['id']}.json.tmp")
    assert calls[1][-1].endswith(f"/outbox/{request['id']}.json")
    assert re.fullmatch("[0-9a-f]{32}", request["id"])


def test_fetch_and_status_download_without_publish(tmp_path, monkeypatch, capsys):
    path = config(tmp_path)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_text('{"schema":1,"threads":[],"content_available":true}', encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.main(["--config", str(path), "fetch"]) == 0
    assert (tmp_path / "local" / "reports.json").exists()
    receipt_id = "a" * 32
    assert client.main(["--config", str(path), "status", receipt_id]) == 0
    assert (tmp_path / "local" / "receipts" / f"{receipt_id}.json").exists()
    assert len(calls) == 2 and all(call[0] == "scp" for call in calls)
    assert client.main(["--config", str(path), "status", "../bad"]) == 1
    assert len(calls) == 2


def test_ambiguous_rename_failure_keeps_request_and_prints_id(tmp_path, monkeypatch, capsys):
    path = config(tmp_path)
    local = tmp_path / "local"
    local.mkdir()
    (local / "reports.json").write_text(json.dumps(sample()), encoding="utf-8")
    markdown = tmp_path / "review.md"
    markdown.write_text("Review", encoding="utf-8")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "ssh":
            raise subprocess.TimeoutExpired(args, 60)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.main(["--config", str(path), "submit", "1", str(markdown)]) == 1
    captured = capsys.readouterr()
    request_id = captured.out.strip()
    assert re.fullmatch("[0-9a-f]{32}", request_id)
    saved = json.loads((local / "submissions" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert saved["id"] == request_id and saved["markdown"] == "Review"
    assert "ssh transfer timed out" in captured.err
    assert len(calls) == 2 and all(call[0] in ("scp", "ssh") for call in calls)
    assert f"check status {request_id} before retrying" in captured.err


def test_transfer_timeout_has_no_command_or_config_output(monkeypatch):
    def time_out(args, **kwargs):
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr(client.subprocess, "run", time_out)
    with pytest.raises(client.ReviewError, match="scp transfer timed out") as error:
        client.transfer(["scp", "private-host:private-path"])
    assert "private-host" not in str(error.value)
