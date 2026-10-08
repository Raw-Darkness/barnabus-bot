import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from barnabus import bug_review as bridge, core


class Forum:
    id = 10
    visible = False

    def __init__(self, guild):
        self.guild = guild

    def permissions_for(self, role):
        return NS(view_channel=self.visible)

    async def archived_threads(self, limit):
        for t in self.archives[:limit]:
            yield t


class Thread:
    id, parent_id, name = 100, 10, 'Loading regression'
    archived = locked = False

    def __init__(self, guild):
        self.guild = guild
        self.messages = [message(102), message(101), message(100)]
        self.send = AsyncMock(return_value=NS(id=999))

    async def history(self, limit):
        for m in self.messages[:limit]:
            yield m

    async def fetch_message(self, mid):
        return next(m for m in self.messages if m.id == mid)


def message(mid, author=42):
    return NS(id=mid, author=NS(id=author), content='Report ' + str(mid),
              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), edited_at=None, attachments=[], embeds=[])


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in dict(BugReviewEnabled=True, BugReviewPostingEnabled=True,
                     BugReviewGuildID=1, BugReviewInternalForumIDs=[10], BugReviewPublicForumIDs=[20],
                     BugReviewDirectory=str(tmp_path), EnableMessageContentIntent=True,
                     BugReviewMessagesPerThread=100, BugReviewThreadsPerForum=100).items():
        monkeypatch.setitem(core.config, k, v)
    guild = NS(id=1, default_role=NS(id=1))
    forum, thread = Forum(guild), Thread(guild)
    forum.archives = []
    guild.active_threads = AsyncMock(return_value=[thread])
    async def fetch(cid):
        return {10: forum, 100: thread}[cid]
    bot = NS(intents=NS(message_content=True), user=NS(id=500), fetch_channel=AsyncMock(side_effect=fetch))
    monkeypatch.setattr(core, 'bot', bot)
    monkeypatch.setattr(bridge.discord, 'ForumChannel', Forum)
    monkeypatch.setattr(bridge.discord, 'Thread', Thread)
    return forum, thread, bot


def request(report):
    return dict(schema=1, id='a'*32, guild_id='1', forum_id='10', thread_id='100',
                report_revision=report['revision'], markdown='# Finding\nSuspected cause; not reproduced.')


def prepare(env):
    report = asyncio.run(bridge.export_thread(env[1], 'internal'))
    snapshot = dict(schema=1, threads=[report])
    return request(report), snapshot, dict(requests={}, threads={})


def test_no_intent_exports_metadata_only(env):
    env[2].intents.message_content = False
    async def fail(**kwargs):
        raise AssertionError('History should not be fetched')
        yield
    env[1].history = fail
    result = asyncio.run(bridge.export_thread(env[1], 'internal'))
    assert not result['content_available']
    assert result['messages'] == [] and result['title']


def test_history_includes_starter_and_marks_truncation(env, monkeypatch):
    monkeypatch.setitem(core.config, 'BugReviewMessagesPerThread', 1)
    report = asyncio.run(bridge.export_thread(env[1], 'internal'))
    assert report['history_truncated']
    assert [m['id'] for m in report['messages']] == ['100', '102']


def test_export_skips_own_suggestions_and_changes_revision_on_edit(env):
    report = asyncio.run(bridge.export_thread(env[1], 'internal'))
    env[1].messages.insert(0, message(103, author=500))
    assert asyncio.run(bridge.export_thread(env[1], 'internal'))['revision'] == report['revision']
    env[1].messages[1].content = 'Still broken'
    assert asyncio.run(bridge.export_thread(env[1], 'internal'))['revision'] != report['revision']


@pytest.mark.parametrize('change', [dict(forum_id='20'), dict(guild_id='2'), dict(id='../bad'),
                                  dict(markdown=''), dict(markdown='x'*65537), dict(report_revision='bad')])
def test_invalid_requests_rejected_before_discord(env, change):
    req, snap, state = prepare(env)
    req.update(change)
    with pytest.raises(ValueError):
        asyncio.run(bridge.publish(req, 'a'*32, snap, state))
    env[2].fetch_channel.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('parent_id', 20), ('locked', True), ('archived', True)])
def test_destination_is_revalidated(env, field, value):
    req, snap, state = prepare(env)
    setattr(env[1], field, value)
    with pytest.raises(ValueError):
        asyncio.run(bridge.publish(req, 'a'*32, snap, state))
    env[1].send.assert_not_awaited()


def test_public_visibility_rejected(env):
    req, snap, state = prepare(env)
    env[0].visible = True
    with pytest.raises(ValueError, match='@everyone'):
        asyncio.run(bridge.publish(req, 'a'*32, snap, state))


def test_stale_live_evidence_rejected(env):
    req, snap, state = prepare(env)
    env[1].messages[0].content = 'New information'
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(bridge.publish(req, 'a'*32, snap, state))
    env[1].send.assert_not_awaited()


def test_post_and_deduplicate_and_update(env):
    req, snap, state = prepare(env)
    first = asyncio.run(bridge.publish(req, 'a'*32, snap, state))
    assert first['status'] == 'posted'
    options = env[1].send.call_args.kwargs
    assert options['allowed_mentions'].everyone is False
    assert options['file'].filename == 'suggested-fix.md'
    reply = NS(id=999, author=NS(id=500), edit=AsyncMock())
    original = env[1].fetch_message
    async def fetch(mid):
        return reply if mid == 999 else await original(mid)
    env[1].fetch_message = fetch
    assert asyncio.run(bridge.publish(req, 'a'*32, snap, state))['status'] == 'unchanged'
    env[1].send.assert_awaited_once()
    req['markdown'] += '\nUpdated validation.'
    assert asyncio.run(bridge.publish(req, 'a'*32, snap, state))['status'] == 'posted'
    reply.edit.assert_awaited_once()
    env[1].send.assert_awaited_once()


def test_disabled_posting_does_not_consume_queue(env, monkeypatch):
    monkeypatch.setitem(core.config, 'BugReviewPostingEnabled', False)
    asyncio.run(bridge.process_outbox())
    env[2].fetch_channel.assert_not_awaited()


def test_outbox_claim_survives_uncertain_send(env):
    req, snap, state = prepare(env)
    path = bridge.root()
    bridge.write_json(path/'reports.json', snap)
    bridge.write_json(path/'outbox'/('a'*32+'.json'), req)
    async def failed_send(*a, **kw):
        assert bridge.read_json(path/'state.json')['requests']['a'*32]['status'] == 'uncertain'
        raise TimeoutError()
    env[1].send.side_effect = failed_send
    asyncio.run(bridge.process_outbox())
    receipt = bridge.read_json(path/'receipts'/('a'*32+'.json'))
    assert receipt['status'] == 'uncertain'
    bridge.write_json(path/'outbox'/('a'*32+'.json'), req)
    asyncio.run(bridge.process_outbox())
    env[1].send.assert_awaited_once()


def test_restart_after_success_does_not_repost(env):
    req, snap, state = prepare(env)
    path = bridge.root()
    bridge.write_json(path/'reports.json', snap)
    bridge.write_json(path/'outbox'/('a'*32+'.json'), req)
    asyncio.run(bridge.process_outbox())
    assert bridge.read_json(path/'receipts'/('a'*32+'.json'))['status'] == 'posted'
    bridge.write_json(path/'outbox'/('a'*32+'.json'), req)
    asyncio.run(bridge.process_outbox())
    env[1].send.assert_awaited_once()


def test_forum_errors_visible_and_archived_threads_included(env, monkeypatch):
    archived = Thread(env[1].guild)
    archived.id = 200
    archived.messages = [message(200)]
    archived.archived = True
    env[0].archives = [archived]
    snapshot = asyncio.run(bridge.export_once())
    assert [r['id'] for r in snapshot['threads']] == ['100', '200']
    assert snapshot['errors'] == [{'forum_id': '20', 'error': 'read_failed'}]


def test_overlapping_forums_fail_closed(env, monkeypatch):
    monkeypatch.setitem(core.config, 'BugReviewPublicForumIDs', [10])
    with pytest.raises(ValueError):
        bridge.settings()

@pytest.mark.parametrize('flag', ['false', 1, [], None])
def test_non_boolean_posting_flag_stays_disabled(env, monkeypatch, flag):
    monkeypatch.setitem(core.config, 'BugReviewPostingEnabled', flag)
    asyncio.run(bridge.process_outbox())
    env[2].fetch_channel.assert_not_awaited()


def test_own_reply_does_not_displace_full_history(env, monkeypatch):
    monkeypatch.setitem(core.config, 'BugReviewMessagesPerThread', 1)
    before = asyncio.run(bridge.export_thread(env[1], 'internal'))
    env[1].messages.insert(0, message(103, author=500))
    after = asyncio.run(bridge.export_thread(env[1], 'internal'))
    assert before['revision'] == after['revision']
    assert [m['id'] for m in after['messages']] == ['100', '102']


def test_invalid_queue_paths_cannot_starve_valid_request(env):
    req, snap, state = prepare(env)
    path = bridge.root()
    bridge.write_json(path/'reports.json', snap)
    for i in range(25):
        (path/'outbox'/f'0-bad-{i}.json').write_text('{}')
    (path/'outbox'/('0'*32+'.json')).mkdir()
    bridge.write_json(path/'outbox'/('a'*32+'.json'), req)
    asyncio.run(bridge.process_outbox())
    assert bridge.read_json(path/'receipts'/('a'*32+'.json'))['status'] == 'posted'


def test_deleted_previous_reply_not_reported_unchanged(env):
    req, snap, state = prepare(env)
    asyncio.run(bridge.publish(req, 'a'*32, snap, state))
    # The fake's missing message lookup raises StopIteration; this must not
    # short-circuit to an 'unchanged' success based only on the old digest.
    with pytest.raises(RuntimeError):
        asyncio.run(bridge.publish(req, 'a'*32, snap, state))
    env[1].send.assert_awaited_once()


def test_both_bridge_flags_default_disabled():
    import json
    from pathlib import Path
    example = json.loads((Path(__file__).parents[1]/'Config.example.json').read_text(encoding='utf-8'))
    assert example['BugReviewEnabled'] is False
    assert example['BugReviewPostingEnabled'] is False
    assert example['BugReviewInternalForumIDs'] == example['BugReviewPublicForumIDs'] == []


def test_cdn_signature_refresh_is_not_changed_evidence(env):
    req, snapshot, state = prepare(env)
    report = snapshot['threads'][0]
    report['messages'][0]['attachments'] = [{'filename': 'bug.png', 'size': 123,
        'url': 'https://cdn.discordapp.com/attachments/1/2/bug.png?ex=old&is=old&hm=old'}]
    before = bridge.revision(report)
    report['messages'][0]['attachments'][0]['url'] = 'https://cdn.discordapp.com/attachments/1/2/bug.png?ex=new&is=new&hm=new'
    assert bridge.revision(report) == before
    report['messages'][0]['attachments'][0]['filename'] = 'different.png'
    assert bridge.revision(report) != before


def test_export_client_submission_receipt_roundtrip(env, tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('review_client_roundtrip', Path(__file__).parents[1]/'tools'/'bug_review.py')
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    snapshot = asyncio.run(bridge.export_once())
    markdown = tmp_path/'finding.md'
    markdown.write_text('# Reproduction\nStatic analysis only.', encoding='utf-8')
    req = client.build_request(snapshot, '100', markdown)
    root = bridge.root()
    bridge.write_json(root/'outbox'/(req['id']+'.json'), req)
    asyncio.run(bridge.process_outbox())
    assert bridge.read_json(root/'receipts'/(req['id']+'.json'))['status'] == 'posted'
    env[1].send.assert_awaited_once()
