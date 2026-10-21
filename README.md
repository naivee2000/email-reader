# Private Email Reader

A local-first macOS desktop assistant for multiple Gmail and Microsoft Outlook accounts. It
creates provider drafts but contains no send-email operation. Messages, facts, summaries,
style examples, and original attachments are retained in encrypted local storage.

## What is implemented

- Gmail and Microsoft Graph OAuth adapters with initial and incremental sync.
- A keyless Apple Mail bridge that discovers locally configured Gmail/Exchange accounts, imports
  raw messages and attachments, and saves unsent drafts through macOS Automation.
- Unified or per-account Primary/Updates inbox categories, Sent, synced Drafts, thread view,
  search, summaries, reply choices, compose, and important-item controls in a PySide6/QML UI.
- SQLCipher production database and AES-256-GCM attachment storage, with secrets in macOS
  Keychain.
- Local extraction for PDF, Word, Excel, PowerPoint, CSV/text, and images. Images and scanned
  PDFs use macOS Vision OCR when PyObjC Vision is installed.
- Hybrid lexical/semantic retrieval with source citations and exact attachment recovery.
- Message attachments can be opened in their original format or exported through a native Save dialog;
  embedded signature images stay in the message and out of the main file list.
- AI can use either the OpenAI Responses API (`store=False`) or the locally installed Codex CLI
  with the user's existing ChatGPT sign-in. Both paths validate structured JSON results.

## Install on macOS

The application targets Python 3.12 through 3.14. SQLCipher is mandatory for production data.

```bash
brew install python@3.12 sqlcipher
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
CFLAGS="-I$(brew --prefix sqlcipher)/include" \
LDFLAGS="-L$(brew --prefix sqlcipher)/lib" \
pip install -e '.[sqlcipher,dev]'
```

Launch from source:

```bash
email-reader
```

On first launch, choose **Automatic**, **Local Codex**, or **OpenAI API key**. The recommended email
setup is **Import from Apple Mail**, which needs no Google or Microsoft developer credentials. An
API key is optional when Codex CLI is installed and authenticated. OAuth and API secrets, when
used, are saved in macOS Keychain and never in project files.

### Apple Mail connection (recommended)

Add each Gmail, Outlook, or Exchange account to the macOS Mail app, then select **Import from Apple
Mail** in Email Reader. macOS asks once whether Email Reader may control Mail. The bridge reads raw
message source and attachments locally and creates drafts with `Mail.save`; it contains no send
operation. The import policy excludes explicitly identified **Promotions** and existing junk/trash,
not Social, Forums, Outlook Other, or mailing lists. Campus announcements and other list mail are
retained. Apple Mail does not consistently expose Gmail categories: when an explicit category is
unavailable, the message is retained rather than guessed to be promotional. Gmail API category
labels are authoritative; Outlook's Focused/Other distinction is not a promotions filter.
Only Inbox, Sent and Drafts are imported; this does not expand into archive or custom folders.
If permission was
declined, enable it in **System Settings → Privacy & Security →
Automation → Email Reader → Mail**.

Inbox opens on **All**; use category tabs to narrow it. Refresh is scheduled shortly after opening,
on return when due, and at five-minute due intervals while running. This is a scheduling target,
not a promise that Apple Mail completes or downloads new mail within five minutes. Mail must itself
be connected and have downloaded the message; Email Reader does not bypass account sign-in.

Each refresh checks newest mail across accounts first, then gives each account one resumable page
at a time. A long scan also receives fresh-head checks without replacing its saved continuation.
Apple Mail sources are looked up by captured identity, not shifting list position. Metadata pages
overlap and do not assume that the first old date means the rest of a folder is old. Completed
originals are not downloaded or extracted again; incomplete originals respect their retry due time.
Explicit promotion decisions are cached for 24 hours under the current policy and remain auditable;
expiry permits changed classifications to be reconsidered.

The promotion-only policy performs a one-time, resumable historical repair within each account's
**original saved import range**. It has a separate checkpoint from live refresh and preserves local
trash, retained originals and corrections. Unknown or formerly excluded originals can recover
without resetting the entire database. Existing saved data and encryption remain in place; schema
updates add durable work/checkpoint records rather than requiring a clean reimport.

Readable messages and original files are committed before slower extraction/OCR and optional API
enrichment. File searchability can therefore follow message arrival. Background indexing resumes
after interruption. Provider requests stay serialized, but local reading, offline search and the AI
lane do not wait for an entire account import. An idle search page or open editor no longer pauses
automatic import; draft contents are preserved. A metadata timeout yields the account after one
bounded request, allowing the others to proceed. Failed accounts back off independently; manual
Refresh remains available. Pause keeps a recoverable page checkpoint instead of dropping its tail.

After two consecutive Mail timeout failures, Email Reader attempts a bounded recovery: check for
open Mail drafts, request a normal quit, force quit only when safe or explicitly confirmed, reopen
Mail, and retry the saved import. Known open drafts and Email Reader's active editor prevent a
restart. If Mail is too frozen to check drafts, a confirmation explains the unsaved-text risk.
Automatic recovery attempts are limited to once per hour, including across app restarts. Login,
permission, and ordinary network errors never trigger force quitting. Recovery never sends mail.

Offline keyword search uses only the encrypted local index and saved subjects/addresses, without
AI or embedding requests. It runs independently of Mail imports, and matching results can be opened
while an import is busy. Superseded queued clicks are skipped so they do not delay the latest choice.

Local document extraction runs in an isolated child process with a 60-second deadline. Cancelling
or closing stops that owned process and cleans its temporary files; an unreadable document does
not discard its encrypted original. Identical original files reuse completed extraction results.
The optional API backend has a 30-second socket timeout and no hidden SDK retries. Cancelled
results cannot replace your answer or draft, although an already submitted API request may still
incur usage. Local Codex remains unchanged and API indexing is not enabled for Codex-only accounts.

Direct Gmail API and Microsoft Graph OAuth remain available under the advanced setup section for
users who have their own registered OAuth clients.

Apple Mail does not expose a dependable change marker for editable Drafts through its scripting
interface. To prioritize Inbox and Sent, routine refresh checks Drafts every 30 minutes; use
**Settings → Check Drafts now** after editing one. Initial imports and interrupted imports also
check Drafts. Direct provider access would be needed to guarantee no repeat body fetch for an
unchanged Draft during those checks.

### Local Codex fallback

Install and authenticate Codex CLI, then select **Local Codex — ChatGPT sign-in** in Settings:

```bash
codex login
codex login status
```

The app runs each request with `codex exec --ephemeral` in an isolated, read-only temporary
directory and validates the result against the same JSON schemas used by the API path. It does not
read Codex credential files itself. "Local Codex" refers to the local client and authentication;
relevant email excerpts are still processed by OpenAI under the selected ChatGPT workspace's data
controls. Codex CLI does not provide an embeddings endpoint, so this mode uses encrypted full-text
retrieval unless an API key is selected.

### Reply and reading improvements

- The warm, compact inbox keeps the reading pane alongside the message list. The account selector
  defaults to All accounts; narrow windows use a navigation rail. **More** contains Summary,
  Important, Mark unread, and reading display options. There is one refresh control and a compact
  activity line with expandable details.
- The top search field accepts either a topic or a full question. Smart search opens a dedicated
  workspace with a brief cited answer, three initial results, expandable Sources, and a side-by-side
  source preview. Additional files and possible matches remain available. Use the search-options
  menu for offline keyword search. Search history and unfinished follow-ups are retained for the
  current app session, separately for each account selection.
- Reply, Reply All, and Forward open an editor below the conversation; Compose uses the reading
  area. CC/BCC can be revealed when needed. Reply Assistant is optional: switch among three tabs,
  then choose **Use this reply** to replace the editor text. Navigation preserves unfinished text;
  closing the editor or app asks before discarding it. The app still only saves drafts, never sends.
- Reply Assistant uses strict structured-output schemas, including all nested fields. A response-format
  failure is reported separately from an expired sign-in; it does not require another login.
- Tone is saved as a short document for each sending account and recipient in the encrypted database
  (`tone_notes`), not as a plaintext file. The first draft builds it from suitable sent examples;
  later drafts reuse the document. On the first draft after 30 days it is refreshed. Exact-recipient
  examples take priority; account-wide examples are used when there is no matching history.
  Expand **Show saved tone note** above the reply choices to inspect it.
- In Compose, type a contact name or part of an address. Suggestions come from local message headers
  for the selected account. Click a suggestion, or use arrow keys and Enter/Tab. Commas support
  multiple recipients. No external contacts service or AI request is used for suggestions.
- Conversations display newest first. Recognizable Gmail/Outlook quoted history is hidden by default;
  **Show full message including quoted history** restores the original. Inline and bottom-posted
  replies remain visible where quote markers distinguish them. No stored message is altered.

Run `python scripts/check_email_ui.py` for a fictional-data UI regression check and
`python scripts/check_search_workspace.py` for focused search-layout checks. On macOS the native
checks require a graphical login session. Use `python scripts/benchmark_navigation.py --help`
to compare navigation timings against a preserved source baseline. The default mode measures
controller signals only; `--render` loads both real interfaces with identical fictional messages
and verifies the message content and pixels before recording click-to-frame timings.

### Google OAuth

Create a Google Cloud **Desktop app** OAuth client, enable the Gmail API, and add its client ID
and client secret in Settings. Requested scopes are `gmail.readonly` and `gmail.compose`.

### Microsoft OAuth

Create a Microsoft Entra **public client/native application**, enable mobile/desktop flows, and
add its client ID and tenant (`common` is suitable for mixed consumer/work accounts). The app
requests delegated `Mail.ReadWrite`, `offline_access`, and `User.Read`; Microsoft Graph requires
`Mail.ReadWrite` to create a draft. The codebase deliberately exposes no send method.

## Runtime data

The default data directory is:

`~/Desktop/Python Files/Email Reader/data/`

It can be changed with `EMAIL_READER_DATA_DIR`. Do not sync or commit this directory. Database
encryption is mandatory except when `EMAIL_READER_TESTING=1` is explicitly set for isolated
automated tests.

## Development

Import/scheduling regression coverage includes `tests/test_sync_steps.py`,
`tests/test_import_policy.py`, and `tests/test_responsive_scheduler.py`: one-page fairness, independent
repair/freshness checkpoints, original retry backoff, retained Social/Forums/Other, cached
Promotions decisions, missing-original placeholders and identity-safe source retrieval. These
tests use fictional providers and do not access live mail or send messages. Native script tests
compile generated AppleScript without executing Mail commands.

All macOS helper processes use the shared fork-free `process_launch` module. Do not add direct
`subprocess`/`os.popen` calls to runtime modules: Python's fork fallback can crash child helpers
after Qt/WebEngine starts native threads. The launcher preserves signed parent identity, private
pipes, unrelated-file-descriptor isolation, and request-owned cancellation. Browser authentication
uses the same launch path without changing the user's default browser.

Run `python scripts/check_process_launch.py` or the packaged executable with
`--check-process-launch` for a native WebEngine/concurrent-helper stress check. It uses fictional
input and real isolated extraction workers, refuses unsafe fork paths, and verifies timeout,
cancellation, and cleanup. It does not unlock data, access Keychain, contact Mail/AI, or send mail.

```bash
pytest
ruff check .
python -m email_reader --demo
```

`--demo` populates an isolated demo database with fictional messages and never connects to an
email provider or OpenAI.

Build the signed-ready application bundle after installing SQLCipher support:

```bash
./scripts/build_macos.sh
```

The result is `dist/Email Reader.app`. This private installation requires the existing
`Email Reader Local Signing` certificate. The Keychain helper pins that certificate and app
identifier; another certificate requires an explicit helper migration. Notarized distribution
still requires an Apple Developer identity.

For a safe update of an existing installation, build with `--stage`, close Email Reader normally,
then run `scripts/install_staged_macos.py` with the exact staged `.app` path. It verifies a cold
encrypted database/WAL and attachment snapshot under `data/recovery/`, verifies a ZIP of the old
application, then replaces the bundle without creating another Applications shortcut. It refuses
to install while Email Reader is running. The signed app's `--check-storage` option validates the
database after migration without contacting Mail, providers, or AI. Recovery copies do not contain
Keychain keys; preserve the existing Keychain/helper when restoring on this Mac.

### One-time Keychain migration

Self-signed macOS Keychain clients receive partition rules containing their exact executable hash.
A stable signing certificate alone did not eliminate prompts after rebuilding. Production now uses
an immutable native helper at `data/security/EmailReaderKeychain`, outside the rebuilt app bundle.
It verifies the calling process's app identifier and pinned signing certificate before any Keychain
access. The app verifies the helper's SHA-256 against a descriptor sealed inside its signed bundle.
Secret values travel only over private standard-input/output pipes, never arguments or logs.

The helper copies each secret on first use from `PrivateEmailReader.Stable.v1` or `PrivateEmailReader`
to `PrivateEmailReader.Helper.v1`, preserving the legacy recovery copy. One macOS approval can be
required per existing secret during migration. Subsequent app builds use the unchanged helper, whose
Keychain fingerprint stays fixed. The build refuses to silently replace a changed helper. Source
checkouts cannot use production Keychain access; use `--demo`. Local Codex mode avoids the unrelated
OpenAI API-key item, and opening Settings does not query it.

`dist/Email Reader.app/Contents/MacOS/Email Reader --check-access` verifies encrypted database access
without provider synchronization or displaying message content. Ordinary system requirements such
as unlocking a locked login Keychain, revoked Mail Automation access, or expired provider sessions
still apply.

The macOS build script creates and verifies a separate candidate first. Only then does it move the
previous installed app into a timestamped backup and install the candidate, so a failed build never
disturbs the working app. If macOS has never authorized the stable build for Keychain or Apple Mail,
one system approval is still required; credentials should only be entered into that macOS dialog.
