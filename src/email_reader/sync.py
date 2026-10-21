from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime

from email_reader.categories import IMPORT_POLICY_VERSION
from email_reader.contracts import SourceRetryRequest, SyncPage, SyncProgress, SyncStepResult
from email_reader.providers.apple_mail import AppleMailProvider
from email_reader.providers.base import EmailProvider, SyncCursorExpiredError, provider_failure_kind
from email_reader.repository import MailRepository

ProgressCallback = Callable[[SyncProgress], None]
PageCallback = Callable[[SyncPage], None]


class SyncCoordinator:
    def __init__(
        self,
        repository: MailRepository,
        fact_extractor: Callable[[str, str], object] | None = None,
        indexer: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository
        self.fact_extractor = fact_extractor
        self.indexer = indexer
        self._paused = False
        # Aggregate counts only; never retain Mail error text or message IDs.
        self.last_original_retry_outcome: dict[str, int] = {}

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def sync_account_step(
        self,
        account_id: str,
        provider: EmailProvider,
        *,
        since: datetime | None,
        progress: ProgressCallback | None = None,
        page_callback: PageCallback | None = None,
        force_full: bool = False,
        job_kind: str = "incremental",
        policy_version: str = IMPORT_POLICY_VERSION,
    ) -> SyncStepResult:
        """Commit at most one provider page, then yield to another account.

        Historical repair has an independent durable cursor and original date
        boundary. It never replaces normal synchronization state. Extraction,
        AI, embeddings and original retries are separate scheduling lanes.
        """
        if self._paused:
            return SyncStepResult(paused=True)
        rows = self.repository.db.query("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not rows:
            raise KeyError(f"Unknown account {account_id}")
        account = rows[0]
        repair = job_kind != "incremental"
        freshness = job_kind == "freshness"
        checkpoint = (
            self.repository.get_import_checkpoint(account_id, job_kind, policy_version)
            if repair and not freshness else None
        )
        if checkpoint and checkpoint["status"] == "complete":
            return SyncStepResult(complete=True)
        if checkpoint:
            since = datetime.fromisoformat(checkpoint["since"]) if checkpoint["since"] else None
        cursor = checkpoint["cursor"] if checkpoint else (None if repair or force_full else account["sync_cursor"])
        page_token = checkpoint["page_token"] if checkpoint else (None if repair else account["sync_page_token"])

        def save(*, next_cursor=cursor, next_page=page_token, complete=False):
            if freshness:
                return  # A repeatable first-page check has no resume cursor.
            if repair:
                self.repository.save_import_checkpoint(
                    account_id, job_kind, policy_version, cursor=next_cursor,
                    page_token=next_page, complete=complete, since=since,
                )
            else:
                self.repository.set_sync_state(
                    account_id, cursor=next_cursor, page_token=next_page, completed=complete,
                )

        if repair and checkpoint is None:
            save()
        provider.set_known_message_ids(self.repository.complete_message_ids(
            account_id, include_drafts=provider.draft_message_ids_are_immutable(),
        ))
        provider.set_known_message_revisions(self.repository.complete_message_revisions(account_id))
        provider.set_known_draft_ids(self.repository.available_draft_ids(account_id))
        provider.set_deferred_source_ids(self.repository.source_retry_not_due_ids(account_id))
        provider.set_excluded_message_ids(self.repository.known_import_exclusions(account_id, policy_version))
        event_id = self.repository.db.execute(
            "INSERT INTO sync_events(account_id,started_at,status) VALUES(?,?,'running')",
            (account_id, self.repository.db.now()),
        ).lastrowid
        processed = 0
        try:
            try:
                page = (
                    provider.recent_head(since=since) if freshness else
                    provider.initial_sync(since=since, page_token=page_token)
                    if page_token or not cursor or repair or force_full else
                    provider.incremental_sync(cursor)
                )
            except SyncCursorExpiredError:
                # Yield before retrying: another account must get its turn.
                if not repair:
                    self.repository.db.execute(
                        "UPDATE accounts SET sync_cursor=NULL,sync_page_token=NULL WHERE id=?",
                        (account_id,),
                    )
                else:
                    save(next_cursor=None, next_page=None)
                self.repository.db.execute(
                    "UPDATE sync_events SET completed_at=?,status='retry',processed_count=0 WHERE id=?",
                    (self.repository.db.now(), event_id),
                )
                return SyncStepResult()
            for message in page.messages:
                if self._paused:
                    break
                self.repository.ingest_message(account_id, message, defer_extraction=True)
                if not message.source_complete:
                    self.repository.original_retry_attempted(account_id, message.provider_id)
                processed += 1
                if progress:
                    progress(SyncProgress(account_id=account_id, stage="messages", completed=processed))
            if self._paused and processed < len(page.messages):
                save()  # Replay this idempotent page, not its uncommitted tail.
                result = SyncStepResult(processed=processed, paused=True)
            else:
                # A repair is additive: never clear current Inbox membership
                # using an older snapshot while foreground sync is advancing.
                if page.inbox_scope_reset and not repair:
                    self.repository.begin_main_inbox_scope_scan(account_id, resume=False)
                if page_callback:
                    page_callback(page)
                self.repository.update_provider_read_states(account_id, page.unchanged_read_states)
                self.repository.update_provider_revisions(account_id, page.unchanged_revisions)
                self.repository.update_provider_labels(
                    account_id, page.unchanged_label_states,
                    preserve_read_state=not page.labels_include_read_state,
                )
                self.repository.remove_provider_folder_roles(account_id, page.removed_folder_roles)
                if not repair:
                    self.repository.capture_main_inbox_ids(account_id, page.inbox_scope_ids)
                self.repository.restore_main_inbox_ids(account_id, page.inbox_scope_ids)
                self.repository.mark_remote_deleted(account_id, page.remote_deleted_ids)
                for identifier in page.filtered_bulk_ids:
                    self.repository.resolve_excluded_original(account_id, identifier)
                self.repository.record_import_exclusions(account_id, page.exclusions, policy_version)
                if page.inbox_scope_complete and not repair:
                    self.repository.reconcile_main_inbox_scope(account_id, since)
                examined = max(page.examined_count, len(page.messages))
                complete = not page.next_page_token
                save(next_cursor=page.next_cursor or cursor, next_page=page.next_page_token, complete=complete)
                if progress:
                    progress(SyncProgress(account_id=account_id, stage="page", completed=processed,
                        details={"examined": examined}))
                result = SyncStepResult(processed=processed, examined=examined,
                    complete=complete, paused=self._paused)
            self.repository.db.execute(
                "UPDATE sync_events SET completed_at=?,status=?,processed_count=? WHERE id=?",
                (self.repository.db.now(), "paused" if result.paused else "completed" if result.complete else "page",
                 processed, event_id),
            )
            return result
        except Exception as exc:
            self.repository.db.execute(
                "UPDATE sync_events SET completed_at=?,status='failed',processed_count=?,error_code=?,safe_message=? WHERE id=?",
                (self.repository.db.now(), processed, type(exc).__name__,
                 "Synchronization page failed; stored progress retained", event_id),
            )
            raise

    def retry_pending_originals(
        self,
        account_id: str,
        provider: EmailProvider,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
        limit: int = 8,
    ) -> int:
        """Recover due originals without scanning mailboxes or advancing cursors."""
        self.last_original_retry_outcome = {}
        if self._paused:
            return 0
        pending = self.repository.pending_originals(account_id, limit=limit, force=force)
        return self._apply_original_retries(account_id, provider, pending, set(), progress, 0)

    def _apply_original_retries(
        self,
        account_id: str,
        provider: EmailProvider,
        pending: list[SourceRetryRequest],
        attempted_in_main: set[str],
        progress: ProgressCallback | None,
        processed: int,
    ) -> int:
        retry_requests = [request for request in pending
            if request.provider_id not in attempted_in_main and self.repository.db.scalar(
                "SELECT count(*) FROM source_retry_queue WHERE account_id=? AND provider_id=?",
                (account_id, request.provider_id), 0)]
        if not retry_requests or self._paused:
            return 0
        outcome = {"requested": len(retry_requests), "recovered": 0,
                   "unresolved": 0, "timed_out": 0, "provider_error": 0}
        self.last_original_retry_outcome = outcome
        if progress:
            progress(SyncProgress(account_id=account_id, stage="originals", completed=processed,
                details={"pending": len(retry_requests)}))
        for request in retry_requests:
            self.repository.original_retry_attempted(account_id, request.provider_id)
        try:
            retries = provider.retry_sources(retry_requests)
        except Exception as error:
            if provider_failure_kind(error) is None:
                raise
            # Keep each failed original in the durable queue with its backoff.
            if isinstance(provider, AppleMailProvider) and "timed out" in str(error).lower():
                outcome["timed_out"] = len(retry_requests)
            else:
                outcome["provider_error"] = len(retry_requests)
            retries = []
        requested_ids = {request.provider_id for request in retry_requests}
        recovered = 0
        for retry in retries:
            if self._paused:
                break
            if retry.provider_id not in requested_ids:
                continue
            if retry.message is not None:
                if retry.message.provider_id != retry.provider_id or not retry.message.source_complete:
                    continue  # Never associate another source with this request.
                message_id = self.repository.ingest_message(account_id, retry.message, defer_extraction=True)
                if self.fact_extractor and retry.message.body_text and not retry.message.is_draft:
                    with suppress(Exception):
                        self.fact_extractor(message_id, retry.message.body_text)
                recovered += 1
            elif retry.excluded:
                self.repository.resolve_excluded_original(account_id, retry.provider_id)
                if retry.exclusion_reason:
                    self.repository.record_import_exclusions(account_id,
                        {retry.provider_id: retry.exclusion_reason}, IMPORT_POLICY_VERSION)
        if self.indexer:
            with suppress(Exception):
                self.indexer()
        outcome["recovered"] = recovered
        outcome["unresolved"] = max(
            0, len(retry_requests) - recovered
            - sum(1 for retry in retries if retry.provider_id in requested_ids and retry.excluded),
        )
        return recovered

    def sync_account(
        self,
        account_id: str,
        provider: EmailProvider,
        *,
        since: datetime | None,
        progress: ProgressCallback | None = None,
        page_callback: PageCallback | None = None,
        force_full: bool = False,
        force_original_retries: bool = False,
    ) -> int:
        account_row = self.repository.db.connection.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not account_row:
            raise KeyError(f"Unknown account {account_id}")
        event = self.repository.db.execute(
            "INSERT INTO sync_events(account_id,started_at,status) VALUES(?,?,'running')",
            (account_id, self.repository.db.now()),
        )
        event_id = event.lastrowid
        processed = 0
        examined = 0
        provider.set_known_message_ids(self.repository.complete_message_ids(
            account_id,
            include_drafts=provider.draft_message_ids_are_immutable(),
        ))
        provider.set_known_message_revisions(
            self.repository.complete_message_revisions(account_id)
        )
        provider.set_known_draft_ids(self.repository.available_draft_ids(account_id))
        provider.set_deferred_source_ids(self.repository.source_retry_not_due_ids(account_id))
        provider.set_excluded_message_ids(self.repository.known_import_exclusions(account_id, IMPORT_POLICY_VERSION))
        pending = self.repository.pending_originals(account_id, force=force_original_retries)
        attempted_in_main: set[str] = set()
        try:
            cursor = None if force_full else account_row["sync_cursor"]
            # A full import still resumes from its durable provider page token.
            # ``force_full`` means ignore the incremental cursor, not discard
            # already completed pages after an interruption.
            page_token = account_row["sync_page_token"]
            if self._paused:
                self.repository.db.execute(
                    "UPDATE sync_events SET completed_at=?,status='paused',processed_count=0 WHERE id=?",
                    (self.repository.db.now(), event_id),
                )
                return 0
            if cursor:
                try:
                    page = (
                        provider.initial_sync(since=since, page_token=page_token)
                        if page_token
                        else provider.incremental_sync(cursor)
                    )
                except SyncCursorExpiredError:
                    cursor = None
                    page = provider.initial_sync(since=since, page_token=None)
            else:
                page = provider.initial_sync(since=since, page_token=page_token)
            while True:
                if self._paused:
                    self.repository.set_sync_state(account_id, cursor=cursor, page_token=page_token)
                    break
                for message in page.messages:
                    if self._paused:
                        break
                    message_id = self.repository.ingest_message(account_id, message, defer_extraction=True)
                    attempted_in_main.add(message.provider_id)
                    if not message.source_complete:
                        self.repository.original_retry_attempted(account_id, message.provider_id)
                    if self.fact_extractor and message.body_text and not message.is_draft:
                        with suppress(Exception):
                            self.fact_extractor(message_id, message.body_text)
                    processed += 1
                    if progress:
                        progress(
                            SyncProgress(
                                account_id=account_id, stage="messages", completed=processed
                            )
                        )
                if self._paused:
                    # Replay this idempotent page, not the next one. Some of its
                    # messages may not have reached encrypted storage yet.
                    self.repository.set_sync_state(account_id, cursor=cursor, page_token=page_token)
                    break
                examined += max(page.examined_count, len(page.messages))
                if progress:
                    progress(
                        SyncProgress(
                            account_id=account_id,
                            stage="page",
                            completed=processed,
                            details={"examined": examined},
                        )
                    )
                if page_callback:
                    page_callback(page)
                if page.inbox_scope_reset:
                    self.repository.begin_main_inbox_scope_scan(account_id, resume=False)
                self.repository.update_provider_read_states(account_id, page.unchanged_read_states)
                self.repository.update_provider_revisions(account_id, page.unchanged_revisions)
                self.repository.update_provider_labels(
                    account_id, page.unchanged_label_states,
                    preserve_read_state=not page.labels_include_read_state,
                )
                self.repository.remove_provider_folder_roles(
                    account_id, page.removed_folder_roles
                )
                self.repository.capture_main_inbox_ids(account_id, page.inbox_scope_ids)
                self.repository.restore_main_inbox_ids(account_id, page.inbox_scope_ids)
                self.repository.mark_remote_deleted(account_id, page.remote_deleted_ids)
                for identifier in page.filtered_bulk_ids:
                    self.repository.resolve_excluded_original(account_id, identifier)
                self.repository.record_import_exclusions(account_id, page.exclusions, IMPORT_POLICY_VERSION)
                if page.inbox_scope_complete:
                    self.repository.reconcile_main_inbox_scope(account_id, since)
                if self.indexer:
                    with suppress(Exception):
                        self.indexer()
                cursor = page.next_cursor or cursor
                self.repository.set_sync_state(
                    account_id,
                    cursor=cursor,
                    page_token=page.next_page_token,
                    completed=not page.next_page_token,
                )
                if not page.next_page_token:
                    break
                page_token = page.next_page_token
                try:
                    page = provider.initial_sync(since=since, page_token=page_token)
                except SyncCursorExpiredError:
                    # A provider can invalidate a delta token between pages.
                    # Replay a full, idempotent import rather than leaving the
                    # stale page token permanently wedged in encrypted state.
                    cursor = None
                    page_token = None
                    page = provider.initial_sync(since=since, page_token=None)
            if not self._paused:
                processed += self._apply_original_retries(
                    account_id, provider, pending, attempted_in_main, progress, processed
                )
            self.repository.db.execute(
                "UPDATE sync_events SET completed_at=?,status=?,processed_count=? WHERE id=?",
                (
                    self.repository.db.now(),
                    "paused" if self._paused else "completed",
                    processed,
                    event_id,
                ),
            )
            return processed
        except Exception as exc:
            self.repository.db.execute(
                """UPDATE sync_events SET completed_at=?,status='failed',processed_count=?,
                   error_code=?,safe_message=? WHERE id=?""",
                (
                    self.repository.db.now(),
                    processed,
                    type(exc).__name__,
                    "Synchronization failed; retry without changing stored cursors",
                    event_id,
                ),
            )
            raise
