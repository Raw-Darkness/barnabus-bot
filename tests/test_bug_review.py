import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from barnabus import bug_review as bridge, core, review_crypto
import time
import os
import json


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

    async def history(self, limit, **kwargs):
        for m in self.messages[:limit]:
            yield m

    async def fetch_message(self, mid):
        return next(m for m in self.messages if m.id == mid)


def message(mid, author=42):
    return NS(id=mid, author=NS(id=author), content='Report ' + str(mid),
              created_at=datetime.now(timezone.utc) - timedelta(days=1), edited_at=None, attachments=[], embeds=[])


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    directory = tmp_path_factory.mktemp("review-keys")
    private, public = directory/'review.private.pem', directory/'review.public.pem'
    review_crypto.generate_keypair(private, public)
    return private, public


@pytest.fixture
def env(monkeypatch, tmp_path, keys):
    for k, v in dict(BugReviewEnabled=True, BugReviewPostingEnabled=True,
                     BugReviewGuildID=1, BugReviewInternalForumIDs=[10], BugReviewPublicForumIDs=[20],
                     BugReviewDirectory=str(tmp_path), BugReviewPublicKeyPath=str(keys[1]), EnableMessageContentIntent=True,
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
                report_revision=report['revision'], markdown='# Finding\nSuspected cause; not reproduced.',
                created_at=time.time(), expires_at=report['expires_at'])


def prepare(env):
    report = asyncio.run(bridge.export_thread(env[1], 'internal'))
    snapshot = dict(schema=2, threads=[report])
    bridge._snapshot_index = snapshot
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
    assert asyncio.run(bridge.submit_request({})) == {"status":"rejected", "reason":"Posting disabled"}
    env[2].fetch_channel.assert_not_awaited()


def test_claim_survives_uncertain_send_without_persisting_markdown(env):
    req, snap, state = prepare(env)
    path = bridge.root()
    async def failed_send(*a, **kw):
        assert bridge.read_json(path/'state.json')['requests']['a'*32]['status'] == 'uncertain'
        raise TimeoutError()
    env[1].send.side_effect = failed_send
    receipt = asyncio.run(bridge.submit_request(req))
    assert receipt['status'] == 'uncertain'
    assert asyncio.run(bridge.submit_request(req)) == receipt
    env[1].send.assert_awaited_once()
    assert not (path/'outbox').exists()
    assert all(b'Suspected cause' not in p.read_bytes() for p in path.rglob('*') if p.is_file())


def test_restart_after_success_does_not_repost(env):
    req, snap, state = prepare(env)
    receipt = asyncio.run(bridge.submit_request(req))
    assert receipt['status'] == 'posted'
    assert asyncio.run(bridge.submit_request(req)) == receipt
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
    asyncio.run(bridge.submit_request({}))
    env[2].fetch_channel.assert_not_awaited()


def test_own_reply_does_not_displace_full_history(env, monkeypatch):
    monkeypatch.setitem(core.config, 'BugReviewMessagesPerThread', 1)
    before = asyncio.run(bridge.export_thread(env[1], 'internal'))
    env[1].messages.insert(0, message(103, author=500))
    after = asyncio.run(bridge.export_thread(env[1], 'internal'))
    assert before['revision'] == after['revision']
    assert [m['id'] for m in after['messages']] == ['100', '102']


def test_legacy_plaintext_queue_is_removed_and_never_published(env):
    req, snap, state = prepare(env)
    path = bridge.root()
    (path/'outbox').mkdir()
    bridge.write_json(path/'reports.json', snap)
    bridge.write_json(path/'outbox'/('a'*32+'.json'), req)
    bridge.purge_server(path)
    assert not (path/'reports.json').exists()
    assert not list((path/'outbox').iterdir())
    env[1].send.assert_not_awaited()


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


@pytest.mark.parametrize('shared', [False, True, 'false'])
def test_export_file_modes_keep_state_private(env, monkeypatch, tmp_path, shared):
    from pathlib import Path
    monkeypatch.setitem(core.config, 'BugReviewSharedAccess', shared)
    seen = {}
    original = Path.chmod
    def chmod(path, mode, **kwargs):
        seen[str(path)] = mode
        return original(path, mode, **kwargs)
    monkeypatch.setattr(Path, 'chmod', chmod)
    path = bridge.root()
    for destination in (path/'reports.enc.json', path/'state.json', path/'receipts'/'a.json'):
        bridge.write_json(destination, {})
    assert seen[str(path/'reports.enc.tmp')] == (0o640 if shared is True else 0o600)
    assert seen[str(path/'receipts'/'a.tmp')] == (0o640 if shared is True else 0o600)
    assert seen[str(path/'state.tmp')] == 0o600
    if shared is True:
        assert seen[str(path)] == 0o2750
        assert seen[str(path/'receipts')] == 0o2750


def test_old_starter_and_recently_edited_old_message_are_excluded(env):
    old, edited, new = env[1].messages[2], env[1].messages[1], env[1].messages[0]
    old.created_at = datetime.now(timezone.utc)-timedelta(days=31)
    edited.created_at = datetime.now(timezone.utc)-timedelta(days=31)
    edited.edited_at = datetime.now(timezone.utc)
    report = asyncio.run(bridge.export_thread(env[1], 'internal'))
    assert [m['id'] for m in report['messages']] == [str(new.id)]
    assert report['expires_at'] == new.created_at.timestamp()+bridge.WINDOW


def test_empty_old_thread_is_metadata_only_and_disk_is_encrypted(env, keys):
    for m in env[1].messages:
        m.created_at = datetime.now(timezone.utc)-timedelta(days=31)
    snapshot = asyncio.run(bridge.export_once())
    assert snapshot['threads'] == []
    assert snapshot['metadata_threads'][0]['id'] == '100'
    assert 'messages' not in snapshot['metadata_threads'][0]
    encrypted = bridge.root()/'reports.enc.json'
    assert 'Loading regression' not in encrypted.read_text(encoding='utf-8')
    assert review_crypto.unseal(bridge.read_json(encrypted), keys[0]) == snapshot
    assert not (bridge.root()/'reports.json').exists()


def test_encrypted_export_expiry_not_refreshed_by_new_scan(env, keys):
    oldest = datetime.now(timezone.utc)-timedelta(days=29)
    env[1].messages[1].created_at = oldest
    first = asyncio.run(bridge.export_once())
    second = asyncio.run(bridge.export_once())
    assert first['expires_at'] == second['expires_at'] == oldest.timestamp()+bridge.WINDOW
    plain = review_crypto.unseal(bridge.read_json(bridge.root()/'reports.enc.json'), keys[0])
    assert plain['threads'][0]['messages']
    assert b'Report 101' not in (bridge.root()/'reports.enc.json').read_bytes()


def test_cleanup_expires_ciphertext_even_with_disabled_feature(env, monkeypatch):
    monkeypatch.setitem(core.config, 'BugReviewEnabled', False)
    p=bridge.root()/'reports.enc.json'
    bridge.write_json(p, {'expires_at':time.time()-1})
    assert bridge.purge_server(bridge.root()) == 1
    assert not p.exists()


def test_expired_submission_rejected(env):
    req, snap, state = prepare(env)
    req['expires_at'] = time.time()-1
    assert asyncio.run(bridge.submit_request(req))['status'] == 'rejected'
    env[1].send.assert_not_awaited()


def test_invalid_config_logs_once_until_config_changes(env, monkeypatch, caplog):
    monkeypatch.setitem(core.config, 'BugReviewGuildID', 0)
    env[2].wait_until_ready=AsyncMock()
    count=0
    env[2].is_closed=lambda:count>=4
    async def sleep(_):
        nonlocal count
        count+=1
        if count == 2:
            core.config['BugReviewGuildID']=-1
            core.config['BugReviewInternalForumIDs']=[]
    monkeypatch.setattr(bridge.asyncio, 'sleep', sleep)
    asyncio.run(bridge.loop())
    assert sum('Bug review unavailable' in r.message for r in caplog.records) == 2


@pytest.mark.skipif(os.name=='nt', reason='Production submission socket is POSIX-only')
def test_unix_submission_socket_roundtrip(env):
    req, snap, state=prepare(env)
    async def run():
        bridge._submit_lock=None
        server=await bridge.start_listener()
        try:
            reader,writer=await asyncio.open_unix_connection(str(bridge.root()/'bridge.sock'))
            writer.write(json.dumps(req).encode()+b'\n')
            await writer.drain()
            result=json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return result
        finally:
            server.close()
            await server.wait_closed()
    assert asyncio.run(run())['status']=='posted'
    env[1].send.assert_awaited_once()


def test_server_cleanup_uses_configured_spool(tmp_path):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('cleanup_helper', Path(__file__).parents[1]/'tools'/'bug_review_cleanup.py')
    helper=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    custom=tmp_path/'custom-spool'
    custom.mkdir()
    (custom/'reports.enc.json').write_text(json.dumps({'expires_at':time.time()-1}))
    config=tmp_path/'private-config.json'
    config.write_text(json.dumps({'BugReviewDirectory':str(custom), 'DiscordToken':'not-used'}))
    assert helper.resolve_directory(config)==custom
    assert helper.purge_server(helper.resolve_directory(config))==1
    assert not (custom/'reports.enc.json').exists()


def test_encrypted_export_client_draft_and_inmemory_publish_roundtrip(env, keys, tmp_path):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('privacy_client_integration', Path(__file__).parents[1]/'tools'/'bug_review.py')
    client=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    old=Thread(env[1].guild)
    old.id=200
    old.messages=[message(200)]
    old.messages[0].created_at=datetime.now(timezone.utc)-timedelta(days=31)
    env[0].archives=[old]
    asyncio.run(bridge.export_once())
    config={'local_dir':tmp_path/'workstation', 'private_key':keys[0], 'public_key':keys[1],
            'encrypted_drive_confirmed':True}
    config['local_dir'].mkdir()
    (config['local_dir']/client.SNAPSHOT).write_bytes((bridge.root()/'reports.enc.json').read_bytes())
    snapshot=client.snapshot(config)
    assert len(client.threads(snapshot))==2
    draft=client.create_draft(config,snapshot,'100')
    draft.write_text('# Finding\nStatic analysis only; original text not quoted.',encoding='utf-8')
    request=client.build_request(snapshot,'100',draft,config['local_dir'],client._ledger(config['local_dir']))
    saved=client.save_request(config,request)
    assert b'Static analysis only' not in saved.read_bytes()
    assert review_crypto.unseal(json.loads(saved.read_text(encoding='utf-8')),keys[0])==request
    receipt=asyncio.run(bridge.submit_request(request))
    assert receipt['status']=='posted'
    assert all('messages' not in row for row in bridge._snapshot_index['threads'])
    assert not (bridge.root()/'outbox').exists()
    client.cleanup(config,now=request['expires_at']+1)
    assert not saved.exists() and not draft.exists()
    assert not (config['local_dir']/client.SNAPSHOT).exists()


@pytest.mark.skipif(os.name=='nt', reason='Production SSH forwarding socket is POSIX-only')
def test_ssh_helper_forwards_unicode_stdin_without_files(env):
    from pathlib import Path
    import sys
    req,snapshot,state=prepare(env)
    req['markdown']='# Finding\nUnicode: 🐉 åäö 日本語'
    helper=Path(__file__).parents[1]/'tools'/'bug_review_send.py'
    async def run():
        bridge._submit_lock=None
        server=await bridge.start_listener()
        try:
            proc=await asyncio.create_subprocess_exec(sys.executable,str(helper),str(bridge.root()/'bridge.sock'),
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await proc.communicate(json.dumps(req,ensure_ascii=False).encode('utf-8'))
            assert proc.returncode==0,err.decode()
            return json.loads(out)
        finally:
            server.close()
            await server.wait_closed()
    assert asyncio.run(run())['status']=='posted'
    assert not (bridge.root()/'outbox').exists()
    assert all(b'Unicode:' not in p.read_bytes() for p in bridge.root().rglob('*') if p.is_file())


def test_created_at_boundary_and_future_timestamps(env, monkeypatch):
    actual=datetime.now(timezone.utc)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return actual
    monkeypatch.setattr(bridge,'datetime',FixedDatetime)
    env[1].messages[0].created_at=actual-timedelta(days=30)
    env[1].messages[1].created_at=actual+timedelta(seconds=1)
    env[1].messages[2].created_at=actual-timedelta(days=30)+timedelta(seconds=1)
    report=asyncio.run(bridge.export_thread(env[1],'internal'))
    assert [m['id'] for m in report['messages']]==['100']
