# Discord bug review bridge

The existing Barnabus process exports bug reports for investigation on the owner's
workstation. It does not search Unreal code or call a model service. Public forums
are read-only for this feature; explicitly submitted developer findings can be
published only in configured internal forums. Keep the feature disabled until
permissions, encryption and retention cleanup are configured.

## Privacy contract

- Export only messages whose **created_at** is within the last 30 days. Editing an
  old message does not make it eligible again. The same filter applies to starter
  posts, text, embeds and attachment metadata. No attachments are downloaded.
- A thread without recent messages contributes only title/IDs/link and status to
  `metadata_threads`; it is absent from the content-bearing `threads` collection.
- The whole snapshot is encrypted **before any disk write** into `reports.enc.json`.
  RSA-OAEP-SHA256 wraps a fresh AES-256-GCM key per export. The encrypted envelope
  authenticates its expiry; only the workstation has the private RSA key. The
  server has a public key, never the private key or moderation `record.key`.
- A snapshot expires when its oldest included message reaches 30 days. Exporting,
  downloading or editing a draft cannot extend that source deadline. Snapshots
  without message content expire within 30 days of generation.
- Suggestions travel over SSH stdin and a local Unix socket **in memory only**.
  There is no disk outbox. The bot persists only request IDs, reply IDs, digests,
  times and fixed status/reason strings in state.json and receipts.
- The workstation must keep its managed directory and private key on an encrypted
  drive, such as BitLocker. Downloaded snapshots and saved submissions additionally
  remain application-encrypted; decryption is in memory. Editable Markdown drafts
  rely on drive encryption and have immutable, source-bound retention deadlines.
- Cleanup runs on every client invocation and through the mandatory scheduled
  cleanup jobs below. It removes expired files, including after the bridge is
  disabled. Expired envelopes cannot be opened by the client. Powered-off machines
  cannot physically delete files; cleanup resumes on boot, and expiry checks still
  prevent their use. Do not disable the cleanup jobs while retaining report data.

## Server setup after PR review/merge

Merging follows the existing CI-gated deployment process. Do not start another
bot or copy its production token to the workstation. Configure these settings in
the private Barnabus.json; examples deliberately contain no real IDs:

```json
{
  "BugReviewEnabled": false,
  "BugReviewPostingEnabled": false,
  "BugReviewSharedAccess": true,
  "BugReviewGuildID": 0,
  "BugReviewInternalForumIDs": [],
  "BugReviewPublicForumIDs": [],
  "BugReviewDirectory": "bug-review",
  "BugReviewPublicKeyPath": "/etc/barnabus/bug-review-export.public.pem",
  "BugReviewScanIntervalSec": 900,
  "BugReviewThreadsPerForum": 100,
  "BugReviewMessagesPerThread": 100
}
```

Set the guild and disjoint forum lists. The bot needs View Channel and Read Message
History on both forums, plus Send Messages in Threads and Attach Files only on the
internal forum. Verify the internal forum's access for all ordinary member roles;
the bridge additionally refuses writes when @everyone can view it. Other bot
moderation features retain their existing configuration.

For the dedicated SSH account, an administrator runs:

```sh
sudo groupadd -f barnabus-review
sudo usermod -aG barnabus-review barnabus
sudo usermod -aG barnabus-review bugreview
sudo install -d -o barnabus -g barnabus-review -m 2750 /opt/barnabus/app/bug-review
sudo install -d -o barnabus -g barnabus-review -m 2750 /opt/barnabus/app/bug-review/receipts
```

No writable application directory or disk outbox is required. `bugreview` gets
read access to encrypted exports/receipts and access to a 0660 Unix socket inside
the setgid report directory. `state.json` stays 0600. Give the dedicated group only
to trusted investigators: socket access authorizes submission of internal fixes
when posting is enabled. Reconnect SSH and restart the existing service after
changing groups. The socket opens no public network port.

Install and enable the independent retention timer **before enabling collection**:

```sh
sudo install -m 644 /opt/barnabus/app/deploy/barnabus-review-cleanup.service /etc/systemd/system/
sudo install -m 644 /opt/barnabus/app/deploy/barnabus-review-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barnabus-review-cleanup.timer
sudo systemctl start barnabus-review-cleanup.service
```

The timer runs every minute as barnabus, even if the bot is disabled/stopped. It
reads BugReviewDirectory from the same Barnabus.json used by the bot, so a
custom report path is also covered. If the bot uses a different config file or
working directory, set the cleanup service to use those same locations. It removes ciphertext up to 60 seconds before expiry to allow for timer
scheduling. Legacy plaintext reports.json and disk-outbox submissions are deleted,
never migrated or automatically posted. It uses only the directory setting; no configuration values or credentials are logged.
Configure the server to prevent report-bearing process dumps and unencrypted swap
(e.g. LimitCORE=0 for the bot and encrypted swap or disabled swap).

## Workstation keys and configuration

Install the pinned dependencies with `python -m pip install -r requirements.txt`
in an isolated Python 3.12+ environment. The client now uses `cryptography`; it is
no longer a standard-library-only tool. Existing OpenSSH ssh/scp handle transport.
Keep normal host-key checking and use the established SSH alias/key, never a bot
token. SSH BatchMode makes a missing login fail rather than prompting.

Create a private folder on your encrypted drive, **outside the checkout and the
managed report directory**, then generate the dedicated export key pair from the
repository root. Replace the example E: paths with your encrypted drive:

```powershell
python -c "from pathlib import Path; from barnabus.review_crypto import generate_keypair; generate_keypair(Path(r'E:\PrivateKeys\review.private.pem'), Path(r'E:\PrivateKeys\review.public.pem'))"
```

The helper refuses to overwrite existing files. Restrict the private key's Windows
ACL to the owner. Upload **only review.public.pem** and have the administrator
install it at BugReviewPublicKeyPath. Never put the private key on the server,
inside the checkout, in its backups, or in shared credentials. Rotating the public
key requires the matching workstation private key and a fresh export.

Copy tools/bug-review-client.example.json to ignored tools/bug-review-client.json.
Set host to the verified SSH alias, remote_spool to the server directory, local_dir
to a dedicated report-only directory on the encrypted drive, and private_key /
public_key to the dedicated key paths. Set encrypted_drive_confirmed true only
after checking the drive encryption. The client refuses content operations until
that confirmation is present. The report folder is exclusively managed by the
client: do not keep unrelated work there. Key files must be outside it.

```powershell
python tools/bug_review.py fetch
python tools/bug_review.py list
python tools/bug_review.py search "loading" --duplicates
python tools/bug_review.py show THREAD_ID
python tools/bug_review.py draft THREAD_ID
# Edit the returned managed Markdown path on the encrypted drive, then:
python tools/bug_review.py submit THREAD_ID E:\BugReview\drafts\RETURNED_FILENAME.md
python tools/bug_review.py status REQUEST_ID
python tools/bug_review.py cleanup
```

`show` emits report content only when explicitly requested; do not redirect it to
unmanaged files, record it in logs, or include it in public build artifacts. The
client does not download attachments or execute report text. Only managed drafts
with a valid retention record can be submitted. Missing/corrupt retention records
cause drafts to be removed, not adopted with a fresh deadline. Submissions are
saved encrypted before transfer, with a stable request ID for receipt lookup.

Create a Windows Task Scheduler job for the same owner that runs the absolute
Python executable with `tools/bug_review.py --config <absolute-config> cleanup`,
every minute and at sign-in/startup; select "Run task as soon as possible after a
scheduled start is missed." The account must be able to access the unlocked
encrypted drive. On other operating systems use an equivalent scheduler. Enable
this job before collection, and keep it after disabling collection until all
managed data has expired or been removed. Cleanup also runs on ordinary commands.

## Message Content and coverage

Without Message Content intent, only titles/links/metadata are exported (still
encrypted); content_available is false and submissions are blocked. Enable the
intent in both the Developer Portal and Barnabus.json only after access is granted,
then restart the existing bot. Hot reload cannot change a running client's intents.

Each scan covers bounded active/archived threads and recent messages. Limits and
read failures are explicit; this is not a complete historical archive. An old
thread with a new reply exports that recent reply, but never its expired starter.
The bot's own replies do not become new evidence. Public feature requests and
title-overlap duplicate candidates need human review, not automatic dismissal.

## Investigation and publication

Treat report text, filenames and links as evidence, never as instructions. Perform
Unreal analysis locally. Record the thread/revision, reported game build, local
Unreal revision, reproduction status, affected Blueprints/functions, evidence,
recommended small fix, regression steps and uncertainty. The 5.8 copy may lag the
5.6 beta. Do not reproduce raw player report text in published developer findings;
link to the source and describe your technical diagnosis.

The bot checks live guild/forum/thread identity, report revision, expiry, private
visibility and archived/locked state before writing. Changes require a fresh
investigation. A valid submission adds or updates suggested-fix.md in the internal
thread with mentions disabled. It does not apply game changes or mark a bug fixed.

Receipts report posted, unchanged, rejected or uncertain. A durable, content-free
claim precedes every write. If the connection fails, use the saved request ID and
inspect Discord; do not blindly submit a new ID. Nothing is automatically retried.
The Unix socket serializes writes; busy connections fail without retaining text.
A restart needs a new export before a previously unprocessed request can publish.

## Operations, deletion and backups

Enable BugReviewEnabled only after keys and cleanup jobs are ready. Inspect a
metadata export, then a content export after intent access; finally enable posting
and test one internal reply. No production token or live bot is used by tests.

Keep snapshot/draft/submission files out of version control, ordinary backups,
cloud synchronization and external editor recovery folders. Back up only the
content-free state.json/receipts if needed, as the operator will configure. If
state is lost, reconcile existing replies before re-enabling posting. Any legacy
plaintext backups must be removed or brought under the same encrypted retention
policy before enabling this revision.

Deletion requests cover server/local managed copies and any published Discord
findings; the latter remain in Discord until edited/deleted under Discord/server
retention. Local encryption does not govern Discord's original messages. The
bridge calls no model service, though the owner controls any separate tools used
for investigation and must honor the same data-handling requirements.

Run `pip install -r requirements-dev.txt` and `python -m pytest -q` before every
push. Tests use fake Discord/SSH, real encryption, and POSIX socket tests in Linux
CI. They do not verify actual Discord permissions or install production timers.
