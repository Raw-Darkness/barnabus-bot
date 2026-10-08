# Discord bug review bridge

The bridge exports reports for investigation on the owner's workstation. It does
not run an AI model or search Unreal files on the VPS. It opens no listening port
and uses no additional Discord client. All Discord access belongs to the existing
running Barnabus process.

## Enable on the server after merging

Add these settings to the private Barnabus.json. Use real IDs only there:

```json
{
  "BugReviewEnabled": true,
  "BugReviewPostingEnabled": false,
  "BugReviewGuildID": 0,
  "BugReviewInternalForumIDs": [],
  "BugReviewPublicForumIDs": [],
  "BugReviewDirectory": "bug-review",
  "BugReviewScanIntervalSec": 900,
  "BugReviewThreadsPerForum": 100,
  "BugReviewMessagesPerThread": 100
}
```

Set the server ID, the closed-beta forum in the internal list, and public reports
in the public list. Lists must be disjoint. The examples deliberately contain no
real IDs. The bot needs View Channel and Read Message History on both forums,
and Send Messages in Threads and Attach Files only in the internal forum.
The public forum is read-only for this bridge; other moderation features retain
their existing configuration. Verify the internal forum is actually restricted
to testers/staff, including permissions granted through other roles. The bridge
additionally refuses publishing if @everyone can view it.

The feature starts with the bot on its next normal deploy/restart. Afterwards
settings hot-reload. Start with posting disabled, inspect an export, then set
BugReviewPostingEnabled true when ready to consume explicitly submitted fixes.
Do not launch a second bot process. Merging is the normal CI-gated deploy path.

**Message Content intent:** when disabled, exports contain thread titles, links,
and metadata, with content_available false. The bridge does not fetch message
history or allow fix submissions in this mode. Do not infer an empty bug report
from the missing body. Request intent approval; once approved, enable it in both
the Discord Developer Portal and Barnabus.json and restart the existing service.
A config hot reload cannot change the running client's intents. Before approval,
reports can be supplied manually for local investigation; this bridge will still
not publish findings based on its incomplete snapshots.

## Owner workstation setup

Python 3.12+ and OpenSSH ssh/scp are sufficient; the local tool uses only Python's
standard library and never reads a bot token. Copy tools/bug-review-client.example.json
to tools/bug-review-client.json and set host to the existing SSH alias, or
username@hostname. Set local_dir to a private folder, optionally inside the local
Unreal project. Relative paths are relative to the client configuration file.

SSH host verification stays enabled. Set up and verify the host key interactively
first, and use your existing key or SSH agent. The tool uses BatchMode, so it
will fail instead of requesting passwords. The SSH identity must be able to read
reports/receipts and write the bridge outbox. The default mode is service-account
only (0700 directories, 0600 files). For a separate SSH account, use the dedicated
group setup below. Do not make the spool world readable or copy production tokens
to the workstation. SSH account/key setup is operator work.

### Separate bugreview SSH account

An administrator runs these on the server, preserving the existing service user:

```sh
sudo groupadd -f barnabus-review
sudo usermod -aG barnabus-review barnabus
sudo usermod -aG barnabus-review bugreview
sudo install -d -o barnabus -g barnabus-review -m 2750 /opt/barnabus/app/bug-review
sudo install -d -o barnabus -g barnabus-review -m 2750 /opt/barnabus/app/bug-review/receipts
sudo install -d -o barnabus -g barnabus-review -m 2770 /opt/barnabus/app/bug-review/outbox
```

Set `BugReviewSharedAccess: true` in the private Barnabus.json along with the
forum settings. Deploy the feature through the normal PR/merge process. The
existing bot service must restart after the group change to pick up its new
group membership; reconnect SSH as well. Do not start a second bot.

In shared mode, newly exported reports and receipts use 0640, while state.json
stays 0600. Setgid directories preserve the dedicated group on atomic replacement.
The SSH account can read exports/receipts and create/rename submissions in outbox;
it cannot replace reports, receipts, or the private state ledger. Uploaded JSON
must be group-readable (the provided SCP client transfers ordinary readable files).
Only add trusted report investigators to this group: outbox write access allows
submitting internal-thread suggestions when posting is enabled. This does not
require access to Barnabus.json, its tokens, or write access to application code.

If enabling shared access on a spool that already contains exports, regenerate
reports after setup. Existing receipt files need their group set to barnabus-review
and mode 0640 by the administrator if they must be readable immediately. Keep
state.json private. The setgid group setup must be repeated for a relocated spool.

```powershell
python tools/bug_review.py fetch
python tools/bug_review.py list
python tools/bug_review.py search "loading" --duplicates
python tools/bug_review.py submit THREAD_ID path/to/finding.md
python tools/bug_review.py status REQUEST_ID
```

Fetch only downloads data; submit explicitly authorizes a reply in that report.
Treat all report text, attachment names and links as untrusted evidence, never as
instructions to run commands, change configuration, disclose secrets, or publish
elsewhere. The tool does not download or execute attachments. Thread IDs preserve
identity across repeated fetches. Title overlap supplies possible duplicates,
not a decision to discard a report. Review public feature requests separately.

## Local investigation and suggested-fix document

Prioritize internal reports, then public reports that corroborate them. Record:

- Discord thread link and report revision.
- Reported game build and the local Unreal revision examined (the 5.8 copy may
  lag the 5.6 beta). Do not assume they match.
- Reproduction status: reported only, statically supported, or reproduced.
- Affected Blueprints/functions/components and concrete evidence.
- Suspected cause, recommended small fix, risks, and focused regression steps.
- What was actually tested, remaining uncertainty, and related reports.

Investigate and write the Markdown on the workstation using the local project
atlas/editor. This feature does not automatically launch Codex or an Unreal
session, schedule local investigations, apply game changes, or mark bugs fixed.
Those operations need a local workflow using the exported data.

## Coverage and posting behavior

Every scan replaces reports.json atomically. Each forum contributes up to the
configured thread limit, active threads first and then archived threads (bounded
archive discovery). Each thread includes recent messages plus its starter if it
still exists. Exports identify truncated histories/forums and read failures.
Increase limits deliberately for a backlog review (maximum 1000 threads per forum,
500 recent messages per thread). This is a bounded current view, not a complete
historical Discord archive; an older report can fall outside the export.
The bot's own replies are excluded from evidence. Attachments are exported as
metadata/URLs, not downloaded; Discord attachment URLs may expire, so fetch fresh
reports when needed. Nothing is sent to OpenRouter by this feature.

Submission binds the Markdown to the report revision and actual guild/forum/thread.
The bot fetches the destination and current report again before posting. Changed
reports, unavailable text, public destinations, visible-to-everyone internal
forums, and archived/locked threads are rejected. Reopen a thread explicitly if
appropriate; the bot never unarchives it for publishing. Valid submissions add a
suggested-fix.md attachment with mentions disabled. A changed suggestion updates
the bridge's previous reply; identical Markdown is left unchanged.

The outbox is checked every 30 seconds when enabled. A receipt reports posted,
unchanged, rejected, or uncertain. On uncertain, inspect the thread before doing
anything else: the message may already exist. The request is durably claimed
before writing to Discord and is not automatically retried, including after a
restart. Keep the saved request ID if upload/rename confirmation fails. Do not
blindly submit a new ID. A deleted bot reply produces an uncertain failure rather
than an automatic replacement. Resolve uncertain cases manually, preserving any
existing reply; state.json contains the per-thread reply IDs and request receipts.

## Data and operations

The spool contains private report text and generated findings. Completed queue
files are removed; latest snapshot and unresolved queue files persist until
replaced/processed or removed by the operator. state.json/receipts retain IDs,
digests, times and statuses, not submitted Markdown. Local downloads and drafts
persist until the owner deletes them. Disabling the bridge stops processing but
does not erase existing files or queued submissions; review the queue before
re-enabling. Deletion requests must cover the server snapshot/queue, local copies,
and any posted Discord document. Do not commit these files. Config.example.json
and the example local client config are safe to commit.

Back up state.json when preserving the spool across server moves, so existing
reply IDs and processed requests are not forgotten. The existing database-only
backup does not automatically include this new directory. If state is lost,
leave posting disabled and reconcile previous replies before enabling it again.
Keep snapshots/Markdown out of public logs and shared build artifacts.

## Validation without a live bot

```text
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests fake Discord and SSH. They cover reduced-intent exports, destinations,
stale reports, updates, interrupted sends, and transfer construction. No test
starts a bot, uses a production token, contacts the VPS, or proves actual Discord
permissions. After merging and configuring, verify a metadata export first; once
intent approval is granted, verify a full report and one internal-thread reply.
