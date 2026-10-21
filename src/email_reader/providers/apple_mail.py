from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from email_reader import process_launch
from email_reader.categories import classify_inbox_headers, inbox_exclusion_reason
from email_reader.config import AppPaths
from email_reader.contracts import (
    Account,
    DraftReceipt,
    DraftRequest,
    MessagePayload,
    ProviderKind,
    SourceRetryRequest,
    SourceRetryResult,
    SyncPage,
)
from email_reader.providers.base import AuthenticationError, EmailProvider, ProviderOperationError
from email_reader.providers.mime import parse_rfc822

PAGE_SIZE = 80
INCREMENTAL_SOURCE_TIMEOUT = 8
DISCOVERY_CACHE_KEY = "apple_mail_discovery_cache_v1"
DISCOVERY_CACHE_SECONDS = 900
RECENT_MAIL_LOOKBACK = timedelta(days=7)
MAIL_OPERATION_LOCK = threading.RLock()
PAGE_OVERLAP = 2


def incremental_cutoff(cursor: str) -> datetime:
    """Mail can download old deliveries after our last local scan completed.

    Reconcile a bounded recent window by message ID, not only five minutes of
    delivery dates. Known originals are still never downloaded again.
    """
    now = datetime.now(UTC)
    try:
        previous = datetime.fromisoformat(cursor).astimezone(UTC)
    except (ValueError, TypeError):
        previous = now
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - RECENT_MAIL_LOOKBACK
    return min(previous - timedelta(minutes=5), window_start)


@dataclass(frozen=True, slots=True)
class AppleMailAccountInfo:
    account_name: str
    email: str
    provider: ProviderKind
    mailboxes: tuple[str, ...]

    @property
    def account_id(self) -> str:
        digest = hashlib.sha256(self.email.lower().encode("utf-8")).hexdigest()[:24]
        return f"apple-mail:{digest}"


def _run_osascript(language: str, script: str, *, timeout: int = 180) -> object:
    try:
        with MAIL_OPERATION_LOCK:
            process = process_launch.run(
                ["/usr/bin/osascript", "-l", language, "-"],
                input=script,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise ProviderOperationError("Apple Mail did not respond before the operation timed out", kind="timeout") from exc
    if process.returncode:
        error = process.stderr.lower()
        if "not authorized" in error or "-1743" in error or "not permitted" in error:
            raise AuthenticationError(
                "Allow Email Reader to control Mail in System Settings → Privacy & Security → Automation"
            )
        if "-1712" in error or "appleevent timed out" in error:
            raise ProviderOperationError("Apple Mail did not respond before the operation timed out", kind="timeout")
        raise ProviderOperationError(f"Apple Mail automation failed (exit {process.returncode})")
    output = process.stdout.strip()
    try:
        return json.loads(output) if output else None
    except ValueError:
        raise ProviderOperationError("Apple Mail returned an invalid response") from None


def _run_jxa(script: str, *, timeout: int = 180) -> object:
    return _run_osascript("JavaScript", script, timeout=timeout)


def _run_applescript(script: str, *, timeout: int = 180) -> object:
    return _run_osascript("AppleScript", script, timeout=timeout)


def _run_mail_script(script: str, *, timeout: int, operation: str) -> object:
    """Attach a fixed, non-content operation name to Mail timeouts only."""
    if operation not in {
        "original_retry", "initial_page", "incremental_metadata",
        "batched_metadata", "incremental_source",
    }:
        raise ValueError("Unknown Mail operation")
    try:
        # Draft saves, recovery, and imports share the same native application.
        # Parallel UI workers must not create competing Apple events.
        with MAIL_OPERATION_LOCK:
            return _run_applescript(script, timeout=timeout)
    except RuntimeError as exc:
        if str(exc) == "Apple Mail did not respond before the operation timed out":
            raise ProviderOperationError(f"Apple Mail timed out during {operation}", kind="timeout") from exc
        raise


def discover_apple_mail_accounts() -> list[AppleMailAccountInfo]:
    result = _run_jxa(
        """
        const Mail = Application("Mail");
        const rows = Mail.accounts().map(account => ({
            name: account.name(),
            emails: account.emailAddresses(),
            enabled: account.enabled(),
            accountType: String(account.accountType()),
            mailboxes: account.mailboxes().map(box => box.name())
        }));
        JSON.stringify(rows);
        """
    )
    accounts: list[AppleMailAccountInfo] = []
    for row in result or []:
        if not row.get("enabled", True):
            continue
        emails = [value for value in row.get("emails", []) if value]
        if not emails:
            continue
        email = emails[0].lower()
        provider = (
            ProviderKind.GMAIL
            if email.endswith(("@gmail.com", "@googlemail.com"))
            else ProviderKind.OUTLOOK
        )
        accounts.append(
            AppleMailAccountInfo(
                account_name=row.get("name") or email,
                email=email,
                provider=provider,
                mailboxes=tuple(row.get("mailboxes", [])),
            )
        )
    return accounts


def cached_apple_mail_accounts(database, required_emails: set[str]) -> tuple[dict[str, AppleMailAccountInfo], bool]:
    """Reuse encrypted Mail account/folder discovery for routine refreshes.

    This cache contains no credentials. A failed sync invalidates it so that the
    next refresh rediscovers disconnected, renamed, or newly reconnected folders.
    """
    required = {email.casefold() for email in required_emails}
    value = database.scalar("SELECT value FROM app_settings WHERE key=?", (DISCOVERY_CACHE_KEY,), default="")
    if value:
        try:
            cached = json.loads(value)
            age = (datetime.now(UTC) - datetime.fromisoformat(cached["checked_at"])).total_seconds()
            entries = cached["accounts"]
            if 0 <= age < DISCOVERY_CACHE_SECONDS and isinstance(entries, list):
                accounts = {
                    item["email"].casefold(): AppleMailAccountInfo(
                        account_name=item["account_name"],
                        email=item["email"],
                        provider=ProviderKind(item["provider"]),
                        mailboxes=tuple(item["mailboxes"]),
                    )
                    for item in entries
                    if isinstance(item, dict)
                }
                if required.issubset(accounts) and all(
                    accounts[email].account_name and accounts[email].mailboxes for email in required
                ):
                    return accounts, True
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    discovered = {item.email.casefold(): item for item in discover_apple_mail_accounts()}
    if required.issubset(discovered):
        payload = {
            "checked_at": datetime.now(UTC).isoformat(),
            "accounts": [
                {"account_name": item.account_name, "email": item.email,
                 "provider": item.provider.value, "mailboxes": list(item.mailboxes)}
                for email, item in discovered.items() if email in required
            ],
        }
        database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (DISCOVERY_CACHE_KEY, json.dumps(payload)),
        )
    return discovered, False


class AppleMailProvider(EmailProvider):
    """Read and draft through the user's locally configured Apple Mail accounts."""

    def __init__(self, account_id: str, info: AppleMailAccountInfo) -> None:
        self.account_id = account_id
        self.info = info
        self._mailbox_names = self._preferred_mailboxes(info)
        self.inbox_provider_ids: set[str] = set()
        self._metadata_cache: dict[tuple, dict] = {}
        self._known_message_ids: set[str] = set()
        self._deferred_source_ids: set[str] = set()
        self._excluded_message_ids: set[str] = set()
        self.include_drafts = True

    def set_known_message_ids(self, identifiers: set[str]) -> None:
        self._known_message_ids = set(identifiers)

    def set_deferred_source_ids(self, identifiers: set[str]) -> None:
        self._deferred_source_ids = set(identifiers)

    def set_excluded_message_ids(self, identifiers: set[str]) -> None:
        self._excluded_message_ids = set(identifiers)

    def retry_sources(self, requests: list[SourceRetryRequest]) -> list[SourceRetryResult]:
        requests = requests[:8]
        if not requests:
            return []
        rows = _run_mail_script(self._retry_script(requests), timeout=30, operation="original_retry")
        if not isinstance(rows, list):
            raise ProviderOperationError("Apple Mail did not return the requested originals")
        expected = {request.provider_id for request in requests}
        output = []
        for row in rows:
            identifier = str(row.get("requestedId", ""))
            if identifier not in expected or not str(row.get("source") or "").strip():
                continue
            message = self._message(row, row["mailbox"])
            if message is not None and (message.provider_id != identifier or not message.source_complete):
                continue
            reason = ""
            if message is None:
                reason = inbox_exclusion_reason(BytesParser(policy=policy.default).parsebytes(
                    str(row["source"]).encode("utf-8", errors="replace"), headersonly=True
                )) or ""
            output.append(SourceRetryResult(provider_id=identifier, message=message,
                excluded=message is None, exclusion_reason=reason))
        return output

    def _retry_script(self, requests: list[SourceRetryRequest]) -> str:
        lines = [
            'use framework "Foundation"', "use scripting additions",
            "set retryRows to current application's NSMutableArray's array()",
            "set isoFormatter to current application's NSISO8601DateFormatter's alloc()'s init()",
            f"set accountName to {json.dumps(self.info.account_name, ensure_ascii=False)}",
        ]
        for request in requests:
            identifier = request.locator.get("message_id") or request.provider_id
            internal_id = request.locator.get("internal_id", "")
            if not internal_id and identifier.isdecimal():
                internal_id, identifier = identifier, ""
            preferred = request.locator.get("mailbox", "")
            role = request.locator.get("role", "")
            mailboxes = sorted(self._mailbox_names, key=lambda box: (
                box != preferred, self._mailbox_role(box) != role))
            lines.extend([
                "set foundRow to missing value",
                f"set wantedId to {json.dumps(identifier, ensure_ascii=False)}",
                f"set bareId to {json.dumps(identifier.strip('<>'), ensure_ascii=False)}",
                f"set wrappedId to {json.dumps('<' + identifier.strip('<>') + '>', ensure_ascii=False)}",
                f"set wantedInternal to {int(internal_id) if internal_id.isdecimal() else -1}",
            ])
            for mailbox in mailboxes:
                date_property = "date sent" if self._mailbox_role(mailbox) in {"sent", "drafts"} else "date received"
                lines.append(f'''
                    if foundRow is missing value then
                        try
                            set mailboxName to {json.dumps(mailbox, ensure_ascii=False)}
                            tell application "Mail"
                                {self._target_mailbox_assignment(mailbox)}
                                set matches to {{}}
                                if wantedInternal > 0 then
                                    try
                                        set matches to (messages of targetMailbox whose id is wantedInternal)
                                        if (count of matches) > 0 then
                                            set candidateMessage to item 1 of matches
                                            set candidateInternetId to message id of candidateMessage
                                            if candidateInternetId is missing value then set candidateInternetId to ""
                                            if wantedId is not "" and candidateInternetId is not wantedId and candidateInternetId is not bareId and candidateInternetId is not wrappedId then
                                                set matches to {{}}
                                            end if
                                        end if
                                    end try
                                end if
                                if (count of matches) is 0 and wantedId is not "" then
                                    set matches to (messages of targetMailbox whose message id is wantedId or message id is bareId or message id is wrappedId)
                                end if
                                if (count of matches) > 0 then
                                    set currentMessage to item 1 of matches
                                    set sourceText to source of currentMessage
                                    if sourceText is not missing value and sourceText is not "" then
                                        set internetId to message id of currentMessage
                                        if internetId is missing value then set internetId to ""
                                        set receivedDate to {date_property} of currentMessage
                                        set receivedText to ""
                                        if receivedDate is not missing value then set receivedText to (isoFormatter's stringFromDate:receivedDate) as text
                                        set foundRow to {{id of currentMessage as text, internetId as text, subject of currentMessage as text, sender of currentMessage as text, not (read status of currentMessage), junk mail status of currentMessage, receivedText, sourceText as text, {json.dumps(mailbox, ensure_ascii=False)}}}
                                    end if
                                end if
                            end tell
                        end try
                    end if
                ''')
            lines.extend([
                "if foundRow is not missing value then",
                "set row to current application's NSMutableDictionary's dictionary()",
                f"row's setObject:{json.dumps(request.provider_id, ensure_ascii=False)} forKey:\"requestedId\"",
            ])
            for index, key in enumerate(["internalId", "messageId", "subject", "sender", "unread", "junk", "received", "source", "mailbox"], 1):
                lines.append(f"row's setObject:(item {index} of foundRow) forKey:\"{key}\"")
            lines.extend(["retryRows's addObject:row", "end if"])
        lines.extend([
            "set jsonData to current application's NSJSONSerialization's dataWithJSONObject:retryRows options:0 |error|:(missing value)",
            "set jsonText to current application's NSString's alloc()'s initWithData:jsonData encoding:(current application's NSUTF8StringEncoding)",
            "return jsonText as text",
        ])
        return "\n".join(lines)

    @classmethod
    def for_email(cls, account_id: str, email: str) -> AppleMailProvider:
        match = next(
            (
                item
                for item in discover_apple_mail_accounts()
                if item.email.lower() == email.lower()
            ),
            None,
        )
        if not match:
            raise AuthenticationError(
                f"{email} is no longer enabled in Apple Mail. Add it in Mail → Add Account."
            )
        return cls(account_id, match)

    @staticmethod
    def _preferred_mailboxes(info: AppleMailAccountInfo) -> list[str]:
        lookup = {name.casefold(): name for name in info.mailboxes}
        preferred: list[str] = []
        if "inbox" in lookup:
            preferred.append(lookup["inbox"])
        for name in ("sent items", "sent mail", "sent messages", "sent"):
            if name in lookup:
                preferred.append(lookup[name])
                break
        for name in ("drafts", "draft"):
            if name in lookup:
                preferred.append(lookup[name])
                break
        return preferred

    def draft_page_token(self) -> str | None:
        for index, mailbox in enumerate(self._mailbox_names):
            if self._mailbox_role(mailbox) == "drafts":
                return json.dumps({"mailbox": index, "offset": 0}, separators=(",", ":"))
        return None

    def begin_main_inbox_capture(self) -> None:
        self.inbox_provider_ids.clear()

    def authenticate(self) -> Account:
        if not any(
            item.email.lower() == self.info.email.lower() for item in discover_apple_mail_accounts()
        ):
            raise AuthenticationError(f"Apple Mail account {self.info.email} is unavailable")
        return Account(
            id=self.account_id,
            provider=self.info.provider,
            email=self.info.email,
            display_name=self.info.account_name,
            color="#EA4335" if self.info.provider == ProviderKind.GMAIL else "#0078D4",
        )

    def initial_sync(self, *, since: datetime | None, page_token: str | None = None) -> SyncPage:
        state = json.loads(page_token) if page_token else {"mailbox": 0, "offset": 0}
        if state.get("incremental_since"):
            since = datetime.fromisoformat(state["incremental_since"]).astimezone(UTC)
        started_at = state.get("started_at") or datetime.now(UTC).isoformat()
        mailbox_index = int(state.get("mailbox", 0))
        offset = int(state.get("offset", 0))
        incremental_page = bool(
            state.get("incremental_since")
            or (since and since.astimezone(UTC) >= datetime.now(UTC) - timedelta(days=2))
        )
        page_size = 10 if incremental_page else PAGE_SIZE
        if not self._mailbox_names:
            raise ProviderOperationError("No supported Inbox, Sent, or Drafts mailbox was found in Apple Mail")
        while mailbox_index < len(self._mailbox_names):
            mailbox = self._mailbox_names[mailbox_index]
            if self._mailbox_role(mailbox) == "drafts" and not self.include_drafts:
                mailbox_index += 1
                offset = 0
                continue
            batch = self._fetch_mailbox(mailbox, offset, page_size, since)
            messages = []
            exclusions = {}
            for row in batch["rows"]:
                if row.get("unchanged"):
                    continue
                message = self._message(row, mailbox)
                if message is not None:
                    messages.append(message)
                else:
                    headers = BytesParser(policy=policy.default).parsebytes(
                        row["source"].encode("utf-8", errors="replace"), headersonly=True
                    )
                    reason = inbox_exclusion_reason(headers)
                    if reason:
                        exclusions[(row.get("messageId") or row["internalId"]).strip("<>")] = reason
            unchanged = {
                (row.get("messageId") or row["internalId"]).strip("<>"): bool(row.get("unread"))
                for row in batch["rows"]
                if row.get("unchanged")
            }
            inbox_ids = [
                (row.get("messageId") or row["internalId"]).strip("<>")
                for row in batch["rows"]
                if self._mailbox_role(mailbox) == "inbox" and (
                    row.get("unchanged") or any(
                        message.provider_id == (row.get("messageId") or row["internalId"]).strip("<>")
                        for message in messages
                    )
                )
            ]
            self.inbox_provider_ids.update(inbox_ids)
            next_offset = offset + int(batch["examined"])
            if not batch["examined"] and offset < int(batch["total"]):
                raise ProviderOperationError("Apple Mail returned an empty page before the mailbox ended; progress retained")
            mailbox_done = bool(batch["reached_since"]) or next_offset >= int(batch["total"])
            if mailbox_done:
                mailbox_index += 1
                offset = 0
            else:
                # A small overlap makes insertion/removal at a page boundary
                # idempotent. Sources are resolved by identity, never position.
                offset = max(offset + 1, next_offset - PAGE_OVERLAP)
            next_token = (
                json.dumps(
                    {"mailbox": mailbox_index, "offset": offset, "started_at": started_at,
                     **({"incremental_since": state["incremental_since"]}
                        if state.get("incremental_since") else {})},
                    separators=(",", ":"),
                )
                if mailbox_index < len(self._mailbox_names)
                else None
            )
            if messages or next_token or unchanged or exclusions:
                return SyncPage(
                    messages=messages,
                    next_page_token=next_token,
                    next_cursor=started_at if not next_token else None,
                    examined_count=int(batch["examined"]),
                    unchanged_read_states=unchanged,
                    inbox_scope_ids=inbox_ids,
                    exclusions=exclusions,
                    filtered_bulk_ids=list(exclusions),
                )
        return SyncPage(messages=[], next_cursor=started_at, examined_count=0)

    def incremental_sync(self, cursor: str) -> SyncPage:
        started_at = datetime.now(UTC).isoformat()
        since = incremental_cutoff(cursor)
        token = json.dumps({"mailbox": 0, "offset": 0, "started_at": started_at,
                            "incremental_since": since.isoformat()}, separators=(",", ":"))
        return self.initial_sync(since=since, page_token=token)

    def recent_head(self, *, since: datetime | None) -> SyncPage:
        token = json.dumps({"mailbox": 0, "offset": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "incremental_since": (since or datetime(1970, 1, 1, tzinfo=UTC)).isoformat()},
            separators=(",", ":"))
        return self.initial_sync(since=since, page_token=token)

    def _fetch_mailbox(self, mailbox: str, offset: int, limit: int, since: datetime | None) -> dict:
        # Metadata is durable even when Mail has not downloaded an original.
        # This same path handles recent checks and all-history imports.
        result = self._fetch_mailbox_metadata(
            mailbox, offset, limit, since or datetime(1970, 1, 1, tzinfo=UTC)
        )
        scan_all_dates = self._mailbox_role(mailbox) == "drafts"
        raw_rows = list(result.get("rows", []))
        needed: list[int] = []
        eligible_rows: list[dict] = []
        for index, row in enumerate(raw_rows):
            if row.get("junk"):
                continue
            identifier = (row.get("messageId") or row["internalId"]).strip("<>")
            if self._mailbox_role(mailbox) == "inbox" and identifier in self._excluded_message_ids:
                continue
            try:
                received = datetime.fromisoformat(row["received"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError):
                received = None
            if since and not scan_all_dates and received and received < since.astimezone(UTC):
                continue
            eligible_rows.append(row)
            if (
                self._mailbox_role(mailbox) != "drafts"
                and identifier in self._known_message_ids
            ) or identifier in self._deferred_source_ids:
                row["unchanged"] = True
            elif row.get("sourcePrefetchAttempted") is not True:
                needed.append(index)
        examined = int(result.get("examined", len(raw_rows)))
        if len(needed) > 10:
            # Large metadata pages are cheap for known mail, but their unknown
            # originals must not monopolize the provider lane. Leave the tail
            # behind the continuation rather than silently deferring/losing it.
            examined = needed[10]
            consumed = {id(item) for item in raw_rows[:examined]}
            eligible_rows = [item for item in eligible_rows if id(item) in consumed]
            needed = needed[:10]
        # At most one source request per page: metadata <=30s + source <=8s.
        # Only eligible, unknown and due originals enter this request.
        for start in range(0, len(needed), 10):
            indexes = needed[start:start + 10]
            selected = [raw_rows[index] for index in indexes]
            try:
                sources = self._fetch_mailbox_sources(
                    mailbox, offset + indexes[0], len(selected), rows=selected
                )
            except AuthenticationError:
                raise
            except ProviderOperationError:
                # Persist the remaining metadata and let durable original
                # retries handle it; do not multiply a timeout across a page.
                break
            for row, source in zip(selected, sources, strict=False):
                if source:
                    row["source"] = source
        return {
            "rows": eligible_rows,
            "total": int(result.get("total", 0)),
            "examined": examined,
            # Only exhaustion, never the first old date, ends a mailbox.
            "reached_since": False,
        }

    def _fetch_mailbox_metadata(
        self, mailbox: str, offset: int, limit: int, since: datetime
    ) -> dict:
        key = (mailbox, offset, limit, since.isoformat())
        result = self._metadata_cache.pop(key, None)
        if result is None:
            # Yield a stalled account after one bounded request; the scheduler
            # can check other accounts before a later independent retry.
            result = _run_mail_script(
                self._metadata_script(mailbox, offset, limit, since),
                timeout=30,
                operation="incremental_metadata",
            )
        if not isinstance(result, dict):
            raise ProviderOperationError("Apple Mail returned invalid metadata; refresh was not advanced")
        for row in result.get("rows", []):
            row["source"] = row.get("prefetchedSource") or row.get("source") or self._metadata_source(row)
        return result

    @classmethod
    def prefetch_recent_metadata(
        cls, tasks: list[tuple[AppleMailProvider, str]], *, prefetch_draft_sources: bool = False,
    ) -> None:
        """One Apple event process for all first pages, not one per mailbox/account."""
        scripts = [
            'use framework "Foundation"',
            "use scripting additions",
            "set batchResults to current application's NSMutableDictionary's dictionary()",
        ]
        keys = []
        for provider, cursor in tasks:
            provider._metadata_cache.clear()
            try:
                since = incremental_cutoff(cursor)
            except ValueError:
                continue
            if datetime.fromisoformat(cursor).astimezone(UTC) < datetime.now(UTC) - timedelta(days=2):
                # An old cursor may cover hundreds of messages. Resume those
                # in small persisted pages instead of one long Mail event.
                continue
            # Incremental sync always uses small resumable pages, including
            # after a long offline gap. A single Mail event stays bounded.
            limit = 10
            for mailbox in provider._mailbox_names:
                if provider._mailbox_role(mailbox) == "drafts" and not provider.include_drafts:
                    continue
                script = provider._metadata_script(
                    mailbox, 0, limit, since, prefetch_draft_sources=prefetch_draft_sources
                )
                # Reuse the exact metadata query; only the outer serialization
                # changes. Draft source batching is diagnostic-only, default
                # off: real ten-run timing did not establish an improvement.
                # No provider flags or content are written.
                script = script.split("set jsonData to", 1)[0]
                script = script.replace('use framework "Foundation"', "").replace(
                    "use scripting additions", ""
                )
                scripts.append(script)
                scripts.append(f'batchResults\'s setObject:resultObject forKey:"{len(keys)}"')
                keys.append((provider, (mailbox, 0, limit, since.isoformat())))
        if not keys:
            return
        scripts.extend(
            [
                "set jsonData to current application's NSJSONSerialization's dataWithJSONObject:batchResults options:0 |error|:(missing value)",
                "set jsonText to current application's NSString's alloc()'s initWithData:jsonData encoding:(current application's NSUTF8StringEncoding)",
                "return jsonText as text",
            ]
        )
        try:
            result = _run_mail_script("\n".join(scripts), timeout=60, operation="batched_metadata")
        except RuntimeError as exc:
            if str(exc) != "Apple Mail timed out during batched_metadata":
                raise
            # Prefetch is an optimization, never a prerequisite. Normal
            # persisted mailbox pages can retry individually after Mail stalls.
            return
        if not isinstance(result, dict):
            raise ProviderOperationError("Apple Mail did not return a complete metadata batch")
        profile_rows = []
        for index, (provider, key) in enumerate(keys):
            if isinstance(result.get(str(index)), dict):
                provider._metadata_cache[key] = result[str(index)]
                if os.environ.get("EMAIL_READER_PROFILE_MAIL") == "1":
                    profile_rows.append({
                        "role": provider._mailbox_role(key[0]),
                        "seconds": round(float(result[str(index)].get("elapsedSeconds", 0)), 3),
                        "resolve": round(float(result[str(index)].get("resolveSeconds", 0)), 3),
                        "first_date": round(float(result[str(index)].get("firstDateSeconds", 0)), 3),
                        "count": round(float(result[str(index)].get("countSeconds", 0)), 3),
                        "dates": round(float(result[str(index)].get("dateSeconds", 0)), 3),
                        "fast_path": bool(result[str(index)].get("fastPath", False)),
                    })
        if profile_rows:
            print("Mail metadata profile: " + json.dumps(profile_rows), flush=True)

    def _metadata_script(
        self, mailbox: str, offset: int, limit: int, since: datetime,
        *, prefetch_draft_sources: bool = False,
    ) -> str:
        account_name = json.dumps(self.info.account_name)
        mailbox_name = json.dumps(mailbox)
        cutoff_text = json.dumps(since.astimezone(UTC).isoformat().replace("+00:00", "Z"))
        date_property = (
            "date sent" if self._mailbox_role(mailbox) in {"sent", "drafts"} else "date received"
        )
        # Mail's scripting order is not guaranteed to match received dates.
        # Keep bounded metadata pages even after an old row; Python filters
        # individual rows and originals are only fetched for eligible IDs.
        scan_all_dates = self._mailbox_role(mailbox) == "drafts"
        prefetch_draft_sources = prefetch_draft_sources and scan_all_dates
        filtered_date_probe = (
            os.environ.get("EMAIL_READER_FILTERED_DATE_PROBE") == "1"
            and self._mailbox_role(mailbox) == "inbox"
        )
        allow_fast_path = "false"
        target_mailbox = self._target_mailbox_assignment(mailbox)
        script = f"""
        use framework "Foundation"
        use scripting additions
        set accountName to {account_name}
        set mailboxName to {mailbox_name}
        set cutoffText to {cutoff_text}
        set scanAllDates to {str(scan_all_dates).lower()}
        set offsetValue to {offset}
        set limitValue to {limit}
        set rawRows to {{}}
        set examinedCount to 0
        set reachedSince to false
        set fastDone to false
        set allowFastPath to {allow_fast_path}
        set filteredDateProbe to {str(filtered_date_probe).lower()}
        set prefetchDraftSources to {str(prefetch_draft_sources).lower()}
        set prefetchedSources to {{}}
        set isoFormatter to current application's NSISO8601DateFormatter's alloc()'s init()
        set startedAt to current application's NSDate's |date|()
        tell application "Mail"
            {target_mailbox}
            set afterResolveAt to current application's NSDate's |date|()
            if allowFastPath then
                try
                    set noRecentMessages to false
                    if filteredDateProbe then
                        -- Round down, never exclude a message on the cutoff second.
                        set cutoffDate to (current application's NSDate's dateWithTimeIntervalSince1970:{int(since.timestamp() // 1)}) as date
                        with timeout of {INCREMENTAL_SOURCE_TIMEOUT} seconds
                            set noRecentMessages to not (exists (messages of targetMailbox whose {date_property} ≥ cutoffDate))
                        end timeout
                    else
                        set firstDate to {date_property} of message 1 of targetMailbox
                        if firstDate is not missing value then
                            set firstISO to isoFormatter's stringFromDate:firstDate
                            set noRecentMessages to ((firstISO's compare:cutoffText) as integer) < 0
                        end if
                    end if
                    if noRecentMessages then
                        set fastDone to true
                        set reachedSince to true
                        set examinedCount to 1
                        if filteredDateProbe then set examinedCount to 0
                        set totalCount to examinedCount
                    end if
                end try
            end if
            set afterFastAt to current application's NSDate's |date|()
            if not fastDone then set totalCount to count messages of targetMailbox
            set afterCountAt to current application's NSDate's |date|()
            set startIndex to offsetValue + 1
            set endIndex to offsetValue + limitValue
            if endIndex > totalCount then set endIndex to totalCount
            if startIndex ≤ totalCount and not fastDone then
                set receivedDates to {date_property} of messages startIndex thru endIndex of targetMailbox
                set afterDatesAt to current application's NSDate's |date|()
                set examinedCount to count of receivedDates
                set sourceCount to examinedCount
                if sourceCount > 0 then
                    set sourceEndIndex to startIndex + sourceCount - 1
                    set internalIds to id of messages startIndex thru sourceEndIndex of targetMailbox
                    set messageIds to message id of messages startIndex thru sourceEndIndex of targetMailbox
                    set subjects to subject of messages startIndex thru sourceEndIndex of targetMailbox
                    set senders to sender of messages startIndex thru sourceEndIndex of targetMailbox
                    set readStates to read status of messages startIndex thru sourceEndIndex of targetMailbox
                    set junkStates to junk mail status of messages startIndex thru sourceEndIndex of targetMailbox
                    if prefetchDraftSources then
                        try
                            with timeout of {INCREMENTAL_SOURCE_TIMEOUT} seconds
                                set prefetchedSources to source of messages startIndex thru sourceEndIndex of targetMailbox
                            end timeout
                        end try
                    end if
                    repeat with i from 1 to sourceCount
                        set internetId to item i of messageIds
                        if internetId is missing value then set internetId to ""
                        set subjectText to item i of subjects
                        if subjectText is missing value then set subjectText to ""
                        set senderText to item i of senders
                        if senderText is missing value then set senderText to ""
                        set receivedDate to item i of receivedDates
                        set receivedText to ""
                        if receivedDate is not missing value then
                            set receivedText to (isoFormatter's stringFromDate:receivedDate) as text
                        end if
                        set sourceText to ""
                        if prefetchDraftSources and i ≤ (count of prefetchedSources) then
                            set sourceValue to item i of prefetchedSources
                            if sourceValue is not missing value then set sourceText to sourceValue as text
                        end if
                        set end of rawRows to {{(item i of internalIds) as text, internetId as text, subjectText as text, senderText as text, not (item i of readStates), item i of junkStates, receivedText, prefetchDraftSources, sourceText}}
                    end repeat
                end if
            end if
        end tell
        if fastDone or startIndex > totalCount then set afterDatesAt to afterCountAt
        set rows to current application's NSMutableArray's array()
        repeat with rawRow in rawRows
            set row to current application's NSMutableDictionary's dictionary()
            row's setObject:(item 1 of rawRow) forKey:"internalId"
            row's setObject:(item 2 of rawRow) forKey:"messageId"
            row's setObject:(item 3 of rawRow) forKey:"subject"
            row's setObject:(item 4 of rawRow) forKey:"sender"
            row's setObject:(item 5 of rawRow) forKey:"unread"
            row's setObject:(item 6 of rawRow) forKey:"junk"
            if (item 7 of rawRow) is not "" then
                row's setObject:(item 7 of rawRow) forKey:"received"
            end if
            if item 8 of rawRow then
                row's setObject:true forKey:"sourcePrefetchAttempted"
                row's setObject:(item 9 of rawRow) forKey:"prefetchedSource"
            end if
            rows's addObject:row
        end repeat
        set resultObject to current application's NSMutableDictionary's dictionary()
        set finishedAt to current application's NSDate's |date|()
        resultObject's setObject:((finishedAt's timeIntervalSinceDate:startedAt) as real) forKey:"elapsedSeconds"
        resultObject's setObject:((afterResolveAt's timeIntervalSinceDate:startedAt) as real) forKey:"resolveSeconds"
        resultObject's setObject:((afterFastAt's timeIntervalSinceDate:afterResolveAt) as real) forKey:"firstDateSeconds"
        resultObject's setObject:((afterCountAt's timeIntervalSinceDate:afterFastAt) as real) forKey:"countSeconds"
        resultObject's setObject:((afterDatesAt's timeIntervalSinceDate:afterCountAt) as real) forKey:"dateSeconds"
        resultObject's setObject:fastDone forKey:"fastPath"
        resultObject's setObject:rows forKey:"rows"
        resultObject's setObject:totalCount forKey:"total"
        resultObject's setObject:examinedCount forKey:"examined"
        resultObject's setObject:reachedSince forKey:"reachedSince"
        set jsonData to current application's NSJSONSerialization's dataWithJSONObject:resultObject options:0 |error|:(missing value)
        set jsonText to current application's NSString's alloc()'s initWithData:jsonData encoding:(current application's NSUTF8StringEncoding)
        return jsonText as text
        """
        return script

    def _fetch_mailbox_sources(
        self, mailbox: str, offset: int, count: int, *, rows: list[dict] | None = None
    ) -> list[str]:
        if count <= 0:
            return []
        account_name = json.dumps(self.info.account_name)
        mailbox_name = json.dumps(mailbox)
        target_mailbox = self._target_mailbox_assignment(mailbox)
        source_query = "set sources to source of messages startIndex thru endIndex of targetMailbox"
        if rows is not None:
            # Bulk metadata and source reads are separate native requests. New
            # deliveries can shift offsets between them; resolve captured IDs.
            lookups = []
            for row in rows:
                internal_id = str(row.get("internalId") or "")
                if not internal_id.isdecimal():
                    raise ProviderOperationError("Apple Mail returned an invalid local message identity")
                lookups.append(f"""
                    set sourceText to ""
                    try
                        set sourceText to source of first message of targetMailbox whose id is {internal_id}
                        if sourceText is missing value then set sourceText to ""
                    end try
                    set end of sources to sourceText
                """)
            source_query = "\n".join(lookups)
        script = f"""
        use framework "Foundation"
        use scripting additions
        set accountName to {account_name}
        set mailboxName to {mailbox_name}
        set startIndex to {offset + 1}
        set endIndex to {offset + count}
        set sources to {{}}
        tell application "Mail"
            {target_mailbox}
            {source_query}
        end tell
        set sourceRows to current application's NSMutableArray's array()
        repeat with sourceText in sources
            if sourceText is missing value then
                sourceRows's addObject:""
            else
                sourceRows's addObject:(sourceText as text)
            end if
        end repeat
        set jsonData to current application's NSJSONSerialization's dataWithJSONObject:sourceRows options:0 |error|:(missing value)
        set jsonText to current application's NSString's alloc()'s initWithData:jsonData encoding:(current application's NSUTF8StringEncoding)
        return jsonText as text
        """
        result = _run_mail_script(
            script, timeout=INCREMENTAL_SOURCE_TIMEOUT, operation="incremental_source"
        )
        return [str(value or "") for value in result] if isinstance(result, list) else []

    def _target_mailbox_assignment(self, mailbox: str) -> str:
        nested_gmail = self.info.provider == ProviderKind.GMAIL and self._mailbox_role(mailbox) in {
            "sent",
            "drafts",
        }
        if not nested_gmail:
            return "set targetMailbox to mailbox mailboxName of account accountName"
        return """
            try
                set targetMailbox to mailbox mailboxName of mailbox "[Gmail]" of account accountName
            on error
                try
                    set targetMailbox to mailbox mailboxName of mailbox "[Google Mail]" of account accountName
                on error
                    set targetMailbox to mailbox mailboxName of account accountName
                end try
            end try
        """

    def _metadata_source(self, row: dict) -> str:
        message = EmailMessage()
        message_id = str(row.get("messageId") or row.get("internalId") or "")
        if message_id:
            message["Message-ID"] = f"<{message_id.strip('<>')}>"
        message["From"] = str(row.get("sender") or "Unknown sender")
        message["To"] = self.info.email
        message["Subject"] = str(row.get("subject") or "(no subject)")
        if row.get("received"):
            message["Date"] = format_datetime(
                datetime.fromisoformat(str(row["received"]).replace("Z", "+00:00"))
            )
        message["X-Email-Reader-Deferred-Source"] = "true"
        message.set_content("")
        return message.as_string()

    def _message(self, row: dict, mailbox: str) -> MessagePayload | None:
        raw = row["source"].encode("utf-8", errors="replace")
        provider_id = (row.get("messageId") or row["internalId"]).strip("<>")
        actual_id = str(BytesParser(policy=policy.default).parsebytes(raw, headersonly=True).get("Message-ID", "")).strip("<> ")
        expected_id = str(row.get("messageId") or "").strip("<> ")
        if actual_id and expected_id and actual_id != expected_id:
            # Apple Mail's positional rows can move if new mail arrives between
            # metadata and source reads. Never assign those bytes to another ID.
            # A moved/replaced native ID must not abort healthy accounts or
            # advance past an unavailable original. Preserve a retryable row.
            raw = self._metadata_source(row).encode("utf-8")
        labels: list[str] = []
        mailbox_role = self._mailbox_role(mailbox)
        if mailbox_role == "inbox":
            category = self._inbox_category(raw)
            if category is None:
                return None
            labels.extend(("INBOX", f"CATEGORY_{category.upper()}"))
            self.inbox_provider_ids.add(provider_id)
        if mailbox_role == "sent":
            labels.append("SENT")
        if mailbox_role == "drafts":
            labels.append("DRAFT")
        if row.get("unread"):
            labels.append("UNREAD")
        thread_id = self._thread_id(raw, provider_id)
        message_id = row.get("messageId") or provider_id
        web_link = f"message://{quote(message_id, safe='')}"
        payload = parse_rfc822(
            raw,
            provider_id=provider_id,
            provider_thread_id=thread_id,
            labels=labels,
            web_link=web_link,
        )
        payload.retry_locator = {"mailbox": mailbox, "internal_id": str(row["internalId"]),
            "message_id": str(row.get("messageId") or ""), "role": mailbox_role}
        if row.get("received"):
            payload.received_at = datetime.fromisoformat(row["received"].replace("Z", "+00:00"))
        if payload.sender_email.lower() == self.info.email.lower() and not payload.is_draft:
            payload.is_sent = True
            if "SENT" not in payload.labels:
                payload.labels.append("SENT")
        return payload

    @staticmethod
    def _inbox_category(raw: bytes) -> str | None:
        headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        return classify_inbox_headers(headers)

    @classmethod
    def _is_bulk_or_social(cls, raw: bytes) -> bool:
        return cls._inbox_category(raw) is None

    @staticmethod
    def _mailbox_role(mailbox: str) -> str:
        key = mailbox.casefold()
        if key == "inbox":
            return "inbox"
        if "sent" in key:
            return "sent"
        if "draft" in key:
            return "drafts"
        return "other"

    @staticmethod
    def _header_datetime(raw: bytes) -> datetime | None:
        headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        value = headers.get("Date")
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _thread_id(raw: bytes, fallback: str) -> str:
        headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        references = str(headers.get("References", "")).split()
        root = references[0] if references else headers.get("In-Reply-To")
        return str(root or headers.get("Message-ID") or fallback).strip("<> ")

    def create_draft(self, request: DraftRequest) -> DraftReceipt:
        if not request.attachments:
            return self._create_draft_with_files(request, [])
        paths = AppPaths.from_environment()
        paths.recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix="draft-attachments-", dir=paths.recovery
        ) as directory:
            files = []
            for index, attachment in enumerate(request.attachments):
                if attachment.content is None:
                    raise ValueError("A draft attachment is unavailable")
                folder = Path(directory) / str(index)
                folder.mkdir(mode=0o700)
                path = folder / (Path(attachment.filename).name or "attachment")
                with path.open("xb") as output:
                    path.chmod(0o600)
                    output.write(attachment.content)
                files.append(str(path))
            return self._create_draft_with_files(request, files)

    def _create_draft_with_files(self, request: DraftRequest, files: list[str]) -> DraftReceipt:
        payload = json.dumps(
            {
                "account": self.info.account_name,
                "sender": self.info.email,
                "subject": request.subject,
                "body": request.body_text,
                "to": request.to,
                "cc": request.cc,
                "bcc": request.bcc,
                "files": files,
            }
        )
        script = f"""
        const Mail = Application("Mail");
        const data = {payload};
        const draft = Mail.OutgoingMessage({{
            sender: data.sender,
            subject: data.subject,
            content: data.body,
            visible: false
        }});
        Mail.outgoingMessages.push(draft);
        for (const address of data.to) draft.toRecipients.push(Mail.ToRecipient({{address}}));
        for (const address of data.cc) draft.ccRecipients.push(Mail.CcRecipient({{address}}));
        for (const address of data.bcc) draft.bccRecipients.push(Mail.BccRecipient({{address}}));
        for (const file of data.files) draft.content.attachments.push(Mail.Attachment({{fileName: Path(file)}}));
        Mail.save(draft);
        JSON.stringify({{id: String(draft.id())}});
        """
        result = _run_jxa(script)
        draft_id = str(result.get("id") or "").strip() if isinstance(result, dict) else ""
        if draft_id.lower() in {"", "none", "null", "undefined"}:
            raise RuntimeError(
                "Apple Mail did not confirm the saved draft; check its Drafts folder before retrying"
            )
        return DraftReceipt(provider_draft_id=draft_id, provider_message_id=draft_id)

    def source_link(self, provider_message_id: str) -> str | None:
        return f"message://{quote(provider_message_id, safe='')}"
