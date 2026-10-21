from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from base64 import b64encode
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Property,
    QFileDevice,
    QIODevice,
    QObject,
    QRunnable,
    QSaveFile,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QImage

from email_reader.context import ApplicationContext
from email_reader.contracts import AttachmentPayload
from email_reader.drafts import draft_request, initial_draft
from email_reader.image_types import is_mail_image, raster_mime
from email_reader.mail_images import MailImageProvider
from email_reader.mail_recovery import is_mail_timeout, recover_mail
from email_reader.providers import (
    AppleMailProvider,
    GmailProvider,
    OutlookProvider,
    cached_apple_mail_accounts,
    discover_apple_mail_accounts,
)
from email_reader.providers.apple_mail import DISCOVERY_CACHE_KEY
from email_reader.providers.base import ProviderOperationError
from email_reader.remote_images import fetch_remote_images
from email_reader.rendering import (
    audited_summary,
    email_document,
    ensure_answer_citations,
    fit_message_images,
    image_data_urls,
    render_message,
    render_source_excerpt,
    safe_link,
)

APPLE_MAIL_SCOPE_VERSION = "main-inbox-v1"
APPLE_MAIL_FOLDER_VERSION = "mail-folders-v2-nested-gmail"
APPLE_MAIL_DRAFT_REFRESH_KEY = "apple_mail_drafts_checked_at_v1"
APPLE_MAIL_DRAFT_REFRESH_INTERVAL = timedelta(minutes=30)


def apple_mail_draft_refresh_due(last_checked: str, now: datetime) -> bool:
    if not last_checked:
        return True
    try:
        previous = datetime.fromisoformat(last_checked).astimezone(UTC)
    except (ValueError, TypeError):
        return True
    age = now.astimezone(UTC) - previous
    return age < timedelta(0) or age >= APPLE_MAIL_DRAFT_REFRESH_INTERVAL


def automatic_sync_retry_delay(interval_seconds: int, failures: int) -> int:
    """Bound failed background refreshes without delaying a manual retry."""
    return min(3600, max(interval_seconds, interval_seconds * 2 ** min(failures, 4)))


class WorkerSignals(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str, str)
    progressed = Signal(str, object)


class Worker(QRunnable):
    def __init__(
        self,
        function: Callable[..., Any],
        *,
        reports_progress: bool = False,
    ) -> None:
        super().__init__()
        self.token = uuid.uuid4().hex
        self.function = function
        self.reports_progress = reports_progress
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            if self.reports_progress:
                result = self.function(
                    lambda update: self.signals.progressed.emit(self.token, update)
                )
            else:
                result = self.function()
            self.signals.succeeded.emit(self.token, result)
        except Exception as exc:
            self.signals.failed.emit(self.token, f"{type(exc).__name__}: {exc}")


class AppController(QObject):
    accountsChanged = Signal()
    threadsChanged = Signal()
    selectedThreadChanged = Signal()
    statusChanged = Signal()
    busyChanged = Signal()
    assistantChanged = Signal()
    settingsChanged = Signal()
    activityChanged = Signal()
    navigationChanged = Signal()
    editorChanged = Signal()
    editorCandidatesChanged = Signal()
    editorSaved = Signal()
    chatChanged = Signal()
    sourcePreviewChanged = Signal()
    sourceFocusChanged = Signal()
    previewAttachmentChanged = Signal()
    mailRestartApprovalRequired = Signal()
    safeToClose = Signal()
    documentReady = Signal(str)
    recipientSuggestionsReady = Signal(str, str, str, "QVariantList")

    def __init__(self, context: ApplicationContext) -> None:
        super().__init__()
        self.context = context
        if hasattr(context, "register_shutdown"):
            context.register_shutdown(self.shutdown)
        self.mail_images = MailImageProvider(context.repository.blobs)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.ai_pool = QThreadPool(self)
        self.ai_pool.setMaxThreadCount(1)
        self.local_pool = QThreadPool(self)
        self.local_pool.setMaxThreadCount(2)
        self.maintenance_pool = QThreadPool(self)
        self.maintenance_pool.setMaxThreadCount(1)
        self.image_pool = QThreadPool(self)
        self.image_pool.setMaxThreadCount(1)
        self.read_pool = QThreadPool(self)
        self.read_pool.setMaxThreadCount(1)
        self.keyword_pool = QThreadPool(self)
        self.keyword_pool.setMaxThreadCount(1)
        self._keyword_jobs: dict[str, dict[str, Any]] = {}
        self._local_jobs: dict[str, dict[str, Any]] = {}
        self._view_token = ""
        self._recipient_tokens: dict[str, str] = {}
        self._document_jobs: set[tuple] = set()
        self._document_cache: OrderedDict[tuple, str] = OrderedDict()
        self._attachment_jobs: set[str] = set()
        self._closing = False
        self._sync_session = None
        self._sync_busy = False
        self._account_schedule: dict[str, dict[str, Any]] = {}
        self._maintenance_running = False
        self._maintenance_cancel = threading.Event()
        self._ai_enrichment_running = False
        self._ai_enrichment_cancel = threading.Event()
        # Jobs retain the QRunnable and route every result through QObject slots
        # on the UI thread. Plain Python closure receivers can otherwise run in
        # the pool thread or disappear before a queued result is delivered.
        self._worker_jobs: dict[str, dict[str, Any]] = {}
        self._image_jobs: dict[str, dict[str, Any]] = {}
        self._read_jobs: dict[str, Worker] = {}
        self._source_jobs: dict[str, Worker] = {}
        self._source_token = ""
        self._source_preview: dict[str, Any] = {}
        self._message_jobs: dict[str, tuple[Worker, str, str]] = {}
        self._read_token = ""
        self._read_cache: OrderedDict[str, tuple[dict[str, Any], int]] = OrderedDict()
        self._read_cache_bytes = 0
        self._message_remote_images: dict[str, dict[str, str]] = {}
        self._message_data_urls: dict[str, dict[str, str]] = {}
        self._accounts: list[dict[str, Any]] = []
        self._accounts_loaded = False
        self._threads: list[dict[str, Any]] = []
        self._selected_thread: dict[str, Any] = {}
        self._focused_message_id = ""
        self._preview_attachment: dict[str, Any] = {}
        self._selected_account = ""
        self._selected_mailbox = "inbox"
        self._selected_category = "all"
        self._keyword_query = ""
        self._keyword_thread_ids: list[str] = []
        self._keyword_token = ""
        self._mailboxes: list[dict[str, Any]] = []
        self._categories: list[dict[str, Any]] = []
        self._view_title = "Primary"
        self._view_count = 0
        self._status = "Ready"
        self._busy = False
        self._assistant_visible = False
        self._sync_failures = 0
        self._mail_timeout_failures = 0
        self._pending_mail_restart = False
        self._next_automatic_sync_at = 0.0
        self._activity: dict[str, Any] = {
            "phase": "idle",
            "title": "Ready",
            "stage": "",
            "detail": "",
            "completed": 0,
            "total": 0,
            "items": 0,
        }
        self._assistant: dict[str, Any] = {}
        self._providers: dict[str, Any] = {}
        self.sync_timings: dict[str, float] = {}
        self._editor_draft: dict = {}
        self._editor_candidates: list[dict] = []
        self._editor_generation = ""
        self._chats: dict[str, list[dict]] = {}
        self._chat_versions: dict[str, str] = {}
        self._search_sessions: dict[str, OrderedDict[str, dict[str, Any]]] = {}
        self._selected_searches: dict[str, str] = {}
        self._pending_search: dict[str, Any] | None = None
        self.reload()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._automatic_sync)
        self.timer.start(self.context.settings.sync_interval_seconds * 1000)
        QTimer.singleShot(2000, self._start_maintenance)
        QTimer.singleShot(10000, self._start_ai_enrichment)

    @Slot(bool)
    def setAssistantVisible(self, visible: bool) -> None:
        self._assistant_visible = visible

    @Slot()
    def _automatic_sync(self) -> None:
        if getattr(self, "_closing", False) or self.context.sync.paused:
            return
        if self.syncBusy:
            self.timer.start(1000)
            return
        if time.monotonic() < self._next_automatic_sync_at:
            self.timer.start(max(1000, int((self._next_automatic_sync_at - time.monotonic()) * 1000)))
            return
        self._sync_all(force_original_retries=False, automatic=True)

    @Property("QVariantList", notify=accountsChanged)
    def accounts(self):
        return self._accounts

    @Property(bool, notify=accountsChanged)
    def accountsLoaded(self) -> bool:
        return getattr(self, "_accounts_loaded", False)

    @Property("QVariantList", notify=threadsChanged)
    def threads(self):
        return self._threads

    @Property("QVariantMap", notify=selectedThreadChanged)
    def selectedThread(self):
        return self._selected_thread

    @Property(str, notify=sourceFocusChanged)
    def focusedMessageId(self) -> str:
        return self._focused_message_id

    @Property("QVariantMap", notify=previewAttachmentChanged)
    def previewAttachment(self):
        return self._preview_attachment

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(int, notify=statusChanged)
    def pendingOriginalCount(self) -> int:
        return getattr(self, "_pending_original_count", 0)

    @Property(int, notify=statusChanged)
    def indexingPending(self) -> int:
        return getattr(self, "_indexing_pending", 0)

    @Property("QVariantMap", notify=chatChanged)
    def searchPreview(self):
        session_id = self._selected_searches.get(self._selected_account)
        session = self._search_sessions.get(self._selected_account, {}).get(session_id, {})
        return session.get("preview", {})

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property("QVariantMap", notify=activityChanged)
    def activity(self):
        return self._activity

    @Property("QVariantMap", notify=assistantChanged)
    def assistantResult(self):
        return self._assistant

    @Property("QVariantMap", notify=editorChanged)
    def editorDraft(self):
        return self._editor_draft

    @Property("QVariantList", notify=editorCandidatesChanged)
    def editorCandidates(self):
        return self._editor_candidates

    @Property("QVariantList", notify=chatChanged)
    def chatMessages(self):
        return self._chats.get(self._selected_account, [])

    @Property("QVariantList", notify=chatChanged)
    def searchSessions(self):
        sessions = self._search_sessions.get(self._selected_account, {})
        return [{"id": item["id"], "title": item["title"]} for item in reversed(sessions.values())]

    @Property(str, notify=chatChanged)
    def selectedSearchId(self) -> str:
        return self._selected_searches.get(self._selected_account, "")

    @Property("QVariantMap", notify=sourcePreviewChanged)
    def sourcePreview(self):
        return self._source_preview

    def _search_session(self) -> dict[str, Any]:
        """Keep legacy account chat aliases pointed at the selected session."""
        scope = self._selected_account
        sessions = self._search_sessions.setdefault(scope, OrderedDict())
        session_id = self._selected_searches.get(scope)
        if session_id not in sessions:
            session_id = uuid.uuid4().hex
            sessions[session_id] = {
                "id": session_id,
                "title": "New search",
                "messages": self._chats.get(scope, []),
                "version": self._chat_versions.get(scope, uuid.uuid4().hex),
            }
            self._selected_searches[scope] = session_id
        session = sessions[session_id]
        self._chats[scope] = session["messages"]
        self._chat_versions[scope] = session["version"]
        return session

    @Slot(str)
    def startSearch(self, question: str) -> None:
        question = question.strip()
        if question and self.aiBusy:
            self._set_status("Finish or cancel the current operation before asking another question")
            return
        scope = self._selected_account
        self._selected_searches.pop(scope, None)
        self._chats[scope] = []
        self._chat_versions[scope] = uuid.uuid4().hex
        self._search_session()
        self.closeSearchSource()
        self.chatChanged.emit()
        if question:
            self.ask(question)

    @Slot(str)
    def askFollowUp(self, question: str) -> None:
        self.ask(question)

    @Slot(str)
    def selectSearch(self, session_id: str) -> None:
        if session_id not in self._search_sessions.get(self._selected_account, {}):
            return
        self._selected_searches[self._selected_account] = session_id
        self._search_session()
        self.closeSearchSource()
        self.chatChanged.emit()

    @Property(bool, notify=busyChanged)
    def aiBusy(self) -> bool:
        return any(job.get("lane") == "ai" for job in getattr(self, "_worker_jobs", {}).values())

    @Property(bool, notify=busyChanged)
    def syncBusy(self) -> bool:
        return getattr(self, "_sync_busy", False)

    @Property(bool, notify=busyChanged)
    def syncPaused(self) -> bool:
        return self.context.sync.paused

    @Property(bool, notify=busyChanged)
    def providerBusy(self) -> bool:
        return self.syncBusy or any(job.get("lane") == "provider" for job in self._worker_jobs.values())

    @Property(bool, notify=busyChanged)
    def draftSaving(self) -> bool:
        return any(job.get("label") == "Saving reviewed draft…" for job in getattr(self, "_worker_jobs", {}).values())

    @Slot()
    def cancelAI(self) -> None:
        if self.aiBusy:
            for job in self._worker_jobs.values():
                if job.get("lane") == "ai":
                    job["cancelled"] = True
            self._editor_generation = ""
            if self._pending_search is not None:
                self._pending_search["version"] = uuid.uuid4().hex
                self._pending_search = None
            self.context.openai.cancel()
            self._set_status("Cancelling AI; your written draft is unchanged")

    @Slot()
    def clearChat(self) -> None:
        session = self._search_session()
        session.update(messages=[], version=uuid.uuid4().hex, title="New search")
        self._search_session()
        self.closeSearchSource()
        self.chatChanged.emit()

    @Slot()
    def closeAssistant(self) -> None:
        self._assistant = {}
        self.assistantChanged.emit()

    @Slot(str)
    def beginDraft(self, mode: str) -> None:
        if getattr(self, "_closing", False):
            return
        if self._editor_draft:
            self.editorChanged.emit()
            return
        token = uuid.uuid4().hex
        self._editor_generation = token
        thread_id = self._selected_thread.get("id")
        accounts, scope = list(self._accounts), self._selected_account
        def work():
            thread = self.context.repository.get_thread_for_read(thread_id) if thread_id else {}
            candidates = [item for item in (thread or {}).get("messages", []) if not item.get("is_draft")]
            if mode != "new" and candidates:
                latest = max(candidates, key=lambda item: item["sent_at"])
                hydrated = self.context.repository.prepare_message_for_read(latest["id"])
                thread["messages"] = [hydrated if item["id"] == latest["id"] else item for item in thread["messages"]]
            if mode in {"reply", "reply_all"} and thread:
                self.context.repository.prepare_reply_thread(thread)
            return initial_draft(mode, thread or {}, accounts, scope)
        def done(draft):
            if self._editor_generation != token or self._editor_draft:
                return
            self._editor_draft = draft
            self._editor_candidates = []
            self.editorCandidatesChanged.emit()
            self.editorChanged.emit()
        self._run_local(work, done,
            failed=lambda: self._set_status("Open a saved email first; its reply headers must be available"))

    @Slot()
    def discardEditor(self) -> None:
        self._editor_draft = {}
        self._editor_candidates = []
        self._editor_generation = ""
        self.editorCandidatesChanged.emit()
        self.editorChanged.emit()

    @Slot("QVariantMap", str)
    def assistEditor(self, form: dict, instruction: str) -> None:
        if not self._editor_draft or self.aiBusy:
            return
        original = dict(self._editor_draft)
        try:
            request = draft_request(form, original)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        token = str(uuid.uuid4())
        self._editor_generation = token
        account_id = form.get("account_id")
        if account_id not in {item["id"] for item in self._accounts}:
            return
        guidance = instruction.strip()
        if request.body_text.strip():
            guidance += (
                "\nCurrent user-written draft (preserve intended meaning):\n"
                + request.body_text
            )

        def work(report):
            report(
                {
                    "stage": "Preparing three editable options",
                    "detail": "Your text will not change until you choose an option",
                }
            )
            if (
                original["mode"] in {"reply", "reply_all"}
            ):
                return self.context.assistant.draft_reply(
                    original["thread_id"], guidance, sending_account_id=account_id,
                    recipients=request.to, draft_subject=request.subject,
                )
            return self.context.assistant.draft_new(
                account_id, request.to, request.subject, guidance
            )

        def done(result):
            if self._editor_generation != token or not self._editor_draft:
                return
            self._editor_candidates = result.model_dump(mode="json")["candidates"]
            self.editorCandidatesChanged.emit()

        self._run("Creating draft options…", work, done, reports_progress=True, ai=True)

    @Slot("QVariantMap")
    def saveEditor(self, form: dict) -> None:
        if not self._editor_draft or self.draftSaving:
            return
        original = dict(self._editor_draft)
        account_id = form.get("account_id")
        if account_id not in {item["id"] for item in self._accounts}:
            self._set_status("Choose a sending account before saving")
            return
        try:
            request = draft_request(form, original)
        except ValueError as exc:
            self._set_status(str(exc))
            return

        def work(report):
            report({"stage": "Saving the draft you reviewed", "detail": "No email will be sent"})
            for identifier in original.get("attachment_ids", []):
                row = self.context.database.connection.execute(
                    "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.id=? AND m.thread_id=?",
                    (identifier, original["thread_id"]),
                ).fetchone()
                if not row or not row["content_digest"]:
                    raise ValueError("A forwarded attachment is unavailable; no draft was saved")
                request.attachments.append(
                    AttachmentPayload(
                        provider_id=row["id"],
                        filename=row["filename"],
                        mime_type=row["mime_type"],
                        content=self.context.repository.blobs.get(row["content_digest"]),
                        size=row["byte_size"],
                    )
                )
            receipt = self._provider(account_id).create_draft(request)
            self.context.repository.record_saved_draft(account_id, receipt, request)
            self.context.database.execute(
                """INSERT INTO draft_records(id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    account_id,
                    original["thread_id"] or None,
                    receipt.provider_draft_id,
                    json.dumps(
                        {
                            "body": request.body_text,
                            "subject": request.subject,
                            "to": request.to,
                            "cc": request.cc,
                            "bcc": request.bcc,
                            "label": form.get("candidate_label", "Manual"),
                        }
                    ),
                    self.context.database.now(),
                ),
            )
            return receipt

        def done(receipt):
            self.discardEditor()
            self.editorSaved.emit()
            self.reload()
            self._set_status("Saved to the selected account’s Drafts. Nothing was sent.")

        self._run("Saving reviewed draft…", work, done, reports_progress=True)

    @Slot(str, str, result="QVariantList")
    def recipientSuggestions(self, account_id: str, query: str):
        return self.context.repository.recipient_suggestions(account_id, query)

    @Property("QVariantList", notify=navigationChanged)
    def mailboxes(self):
        return self._mailboxes

    @Property("QVariantList", notify=navigationChanged)
    def categories(self):
        return self._categories

    @Property(str, notify=navigationChanged)
    def selectedMailbox(self) -> str:
        return self._selected_mailbox

    @Property(str, notify=navigationChanged)
    def selectedCategory(self) -> str:
        return self._selected_category

    @Property(str, notify=navigationChanged)
    def selectedAccount(self) -> str:
        return self._selected_account

    @Property(str, notify=navigationChanged)
    def viewTitle(self) -> str:
        return self._view_title

    @Property(int, notify=navigationChanged)
    def viewCount(self) -> int:
        return self._view_count

    @Property("QVariantMap", notify=settingsChanged)
    def settings(self):
        # The Settings screen must not unlock Keychain merely to draw itself.
        configured = getattr(self, "_api_configured", False)
        return {
            "aiBackend": self.context.settings.ai_backend,
            "activeAiBackend": getattr(self, "_active_backend", self.context.settings.ai_backend),
            "localCodexAvailable": bool(self.context.openai.codex_executable),
            "responseModel": self.context.settings.response_model,
            "extractionModel": self.context.settings.extraction_model,
            "monthlyLimit": self.context.settings.monthly_limit_usd,
            "openaiConfigured": configured,
            "needsSetup": not self._accounts,
        }

    def _set_status(self, value: str) -> None:
        self._status = value
        self.statusChanged.emit()

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        self.busyChanged.emit()

    def _set_activity(self, **changes: Any) -> None:
        activity = dict(self._activity)
        activity.update(changes)
        self._activity = activity
        self.activityChanged.emit()

    def _run(
        self, label: str, function: Callable[..., Any],
        on_success: Callable[[Any], None], *, reports_progress: bool = False,
        completion_label: str | None = None, ai: bool = False,
    ) -> None:
        if getattr(self, "_closing", False):
            return
        if ai and self.aiBusy:
            self._set_status("Finish or cancel the current AI request first")
            return
        if ai:
            if getattr(self, "_ai_enrichment_running", False):
                self._ai_enrichment_cancel.set()
                self.context.openai.cancel()
            def work(*args):
                if getattr(self, "_closing", False) or self._worker_jobs.get(worker.token, {}).get("cancelled"):
                    return None
                # Reset only when this exclusive lane actually starts, never
                # while an earlier cancelled background request is still running.
                self.context.openai.reset_cancellation()
                return function(*args)
        else:
            work = function
        worker = Worker(work, reports_progress=reports_progress)
        self._worker_jobs[worker.token] = {
            "worker": worker, "label": label, "lane": "ai" if ai else "provider",
            "state": "queued", "on_success": on_success, "completion_label": completion_label,
        }
        self._set_busy(True)
        self._set_status(label)
        self._set_activity(phase="running", title=label.rstrip("…"), stage="Starting",
                           detail="", completed=0, total=0, items=0)
        worker.signals.progressed.connect(self._worker_progressed)
        worker.signals.succeeded.connect(self._worker_succeeded)
        worker.signals.failed.connect(self._worker_failed)
        pool = self.ai_pool if ai else self.pool
        if ai or label == "Saving reviewed draft…":
            pool.start(worker, 10)
        else:
            pool.start(worker)

    def _run_local(self, function, done, *, failed=None, pool=None) -> None:
        if getattr(self, "_closing", False):
            return
        if not hasattr(self, "_local_jobs"):
            # Non-UI diagnostic probes construct a deliberately minimal controller.
            try:
                done(function())
            except Exception:
                if failed:
                    failed()
                else:
                    raise
            return
        worker = Worker(function)
        self._local_jobs[worker.token] = {"worker": worker, "done": done, "failed": failed}
        worker.signals.succeeded.connect(self._local_succeeded)
        worker.signals.failed.connect(self._local_failed)
        (pool or self.local_pool).start(worker)

    @Slot(str, object)
    def _local_succeeded(self, token: str, result: object) -> None:
        job = self._local_jobs.pop(token, None)
        if job and not self._closing:
            job["done"](result)

    @Slot(str, str)
    def _local_failed(self, token: str, _error: str) -> None:
        job = self._local_jobs.pop(token, None)
        if job and not self._closing:
            if job["failed"]:
                job["failed"]()
            else:
                self._set_status("This local operation could not finish; please retry")

    @Slot(str, str, str)
    def requestRecipientSuggestions(self, account_id: str, query: str, field_token: str) -> None:
        request_id = uuid.uuid4().hex
        self._recipient_tokens[field_token] = request_id
        def done(results):
            if self._recipient_tokens.get(field_token) == request_id:
                self.recipientSuggestionsReady.emit(field_token, account_id, query, results)
        self._run_local(
            lambda: self.context.repository.recipient_suggestions(account_id, query)
            if self._recipient_tokens.get(field_token) == request_id else [], done
        )

    def _start_maintenance(self) -> None:
        if self._closing or self._maintenance_running:
            return
        self._maintenance_running = True
        def work():
            return self.context.run_maintenance(should_cancel=self._maintenance_cancel.is_set, limit=4)
        def done(result):
            self._maintenance_running = False
            if result and result.get("completed", 0):
                self.reload()
            QTimer.singleShot(2000 if result and result.get("requires_more", False) else 60000,
                              self._start_maintenance)
        def failed():
            self._maintenance_running = False
            QTimer.singleShot(60000, self._start_maintenance)
        self._run_local(work, done, failed=failed, pool=self.maintenance_pool)

    def _start_ai_enrichment(self) -> None:
        """Optional API indexing yields the exclusive AI lane to user requests."""
        if self._closing:
            return
        if (self.aiBusy or self._ai_enrichment_running or self.providerBusy
                or getattr(self, "_active_backend", "") != "api"):
            QTimer.singleShot(60000, self._start_ai_enrichment)
            return
        self._ai_enrichment_running = True
        self._ai_enrichment_cancel.clear()
        def work():
            if self._closing or self._ai_enrichment_cancel.is_set():
                return {}
            self.context.openai.reset_cancellation()
            return self.context.process_ai_enrichment(
                should_cancel=self._ai_enrichment_cancel.is_set, limit=1)
        def done(_result=None):
            self._ai_enrichment_running = False
            QTimer.singleShot(60000, self._start_ai_enrichment)
        self._run_local(work, done, failed=done, pool=self.ai_pool)

    @Slot()
    def requestCloseWhenSafe(self) -> None:
        self._closing = True
        self.timer.stop()
        self.context.sync.pause()
        self.context.openai.cancel()
        self._maintenance_cancel.set()
        self._ai_enrichment_cancel.set()
        self._read_token = self._source_token = self._view_token = ""
        self._clear_keyword_search()
        for job in self._image_jobs.values():
            job["cancel"].set()
        self._set_status("Closing safely — saving the current checkpoint; nothing will be sent")
        self._poll_safe_close()

    def _poll_safe_close(self) -> None:
        pools = (self.pool, self.ai_pool, self.local_pool, self.read_pool,
                 self.keyword_pool, self.image_pool, self.maintenance_pool)
        if any(pool.activeThreadCount() for pool in pools):
            QTimer.singleShot(100, self._poll_safe_close)
            return
        self.safeToClose.emit()

    def shutdown(self) -> None:
        self._closing = True
        self.timer.stop()
        self.context.sync.pause()
        self.context.openai.cancel()
        self._maintenance_cancel.set()
        self._ai_enrichment_cancel.set()
        self._read_token = self._source_token = self._view_token = ""
        self._clear_keyword_search()
        for job in self._image_jobs.values():
            job["cancel"].set()
        for pool in (self.pool, self.ai_pool, self.local_pool, self.read_pool,
                     self.keyword_pool, self.image_pool, self.maintenance_pool):
            pool.clear()
            pool.waitForDone()
    @Slot(str, object)
    def _worker_progressed(self, token: str, update: object) -> None:
        job = self._worker_jobs.get(token)
        if not job or job.get("cancelled") or getattr(self, "_closing", False) or not isinstance(update, dict):
            return
        self._set_activity(phase="running", **update)
        stage = str(update.get("stage") or job["label"])
        detail = str(update.get("detail") or "")
        self._set_status(f"{stage} — {detail}" if detail else stage)

    @Slot(str, object)
    def _worker_succeeded(self, token: str, result: Any) -> None:
        job = self._worker_jobs.pop(token, None)
        if not job:
            return
        if job.get("cancelled") or getattr(self, "_closing", False):
            self._set_busy(bool(self._worker_jobs) or getattr(self, "_sync_busy", False))
            if not getattr(self, "_closing", False):
                self._set_status("AI cancelled; your draft text is unchanged")
                self._set_activity(phase="cancelled", title="AI cancelled", stage="Stopped", detail="")
            return
        if job["label"] == "Syncing all accounts…":
            self._sync_failures = 0
            self._mail_timeout_failures = 0
            self._next_automatic_sync_at = time.monotonic() + self.context.settings.sync_interval_seconds
        self._set_busy(bool(self._worker_jobs) or getattr(self, "_sync_busy", False))
        status_before = self._status
        job["on_success"](result)
        if self._worker_jobs or getattr(self, "_sync_busy", False):
            return
        label = str(job["label"])
        final_status = (
            job["completion_label"]
            or (self._status if self._status != status_before else "")
            or f"Finished — {label.rstrip('…')}"
        )
        self._set_status(final_status)
        if self._activity.get("phase") in {"paused", "error"}:
            return
        self._set_activity(
            phase="complete",
            title=final_status,
            stage="Finished",
            detail="",
        )

    @Slot(str, str)
    def _worker_failed(self, token: str, message: str) -> None:
        job = self._worker_jobs.pop(token, None)
        if not job:
            return
        if job.get("cancelled") or getattr(self, "_closing", False):
            self._set_busy(bool(self._worker_jobs) or getattr(self, "_sync_busy", False))
            if not getattr(self, "_closing", False):
                self._set_status("AI cancelled; your draft text is unchanged")
                self._set_activity(phase="cancelled", title="AI cancelled", stage="Stopped", detail="")
            return
        if job["label"] == "Refreshing mail page…":
            self._sync_busy = False
            self._sync_session = None
            self.context.sync.pause()
            message = "Import stopped safely after a local storage error; saved mail is unchanged"
        if job["label"] == "Searching your email…":
            self._pending_search = None
        if job["label"] == "Syncing all accounts…":
            self._sync_failures += 1
            self._mail_timeout_failures = (
                getattr(self, "_mail_timeout_failures", 0) + 1 if is_mail_timeout(message) else 0
            )
            self._next_automatic_sync_at = time.monotonic() + automatic_sync_retry_delay(
                self.context.settings.sync_interval_seconds, self._sync_failures
            )
            # An account or mailbox may have changed since the last successful
            # refresh. The next attempt must rediscover it rather than loop on
            # a stale account/folder cache.
            self.context.database.execute(
                "DELETE FROM app_settings WHERE key=?", (DISCOVERY_CACHE_KEY,)
            )
            self.reload()  # Other accounts may have completed before a failure.
        self._set_busy(bool(self._worker_jobs) or getattr(self, "_sync_busy", False))
        self._set_status(message)
        self._set_activity(
            phase="cancelled" if message.startswith("OperationCancelled:") else "error",
            title="Operation needs attention",
            stage="Stopped",
            detail=message,
        )
        if job["label"] == "Syncing all accounts…" and self._mail_timeout_failures >= 2:
            QTimer.singleShot(0, self.recoverAppleMail)

    @Slot()
    def recoverAppleMail(self) -> None:
        self._recover_mail(confirmed_force=False)

    @Slot()
    def confirmMailRestart(self) -> None:
        if self._pending_mail_restart:
            self._recover_mail(confirmed_force=True)

    @Slot()
    def cancelMailRestart(self) -> None:
        self._pending_mail_restart = False

    def _recover_mail(self, *, confirmed_force: bool) -> None:
        if self.providerBusy or self._editor_draft:
            self._set_status("Mail recovery postponed while a draft is open or mail access is active")
            return
        self._pending_mail_restart = False
        self._mail_timeout_failures = 0

        def done(outcome):
            if outcome == "restarted":
                self.context.database.execute(
                    "DELETE FROM app_settings WHERE key=?", (DISCOVERY_CACHE_KEY,)
                )
                self._providers.clear()
                self._next_automatic_sync_at = 0.0
                self._set_status("Apple Mail restarted — resuming saved import shortly")
                QTimer.singleShot(3000, self._automatic_sync)
            elif outcome == "approval_required":
                self._pending_mail_restart = True
                self._set_status("Mail is not responding; confirm a force quit to resume importing")
                self.mailRestartApprovalRequired.emit()
            elif outcome == "drafts_open":
                self._set_status("Mail restart skipped: save and close open Apple Mail drafts, then Refresh")
            else:
                self._set_status("Mail still needs attention; automatic restarts are limited to once per hour")

        self._run("Recovering Apple Mail…", lambda: recover_mail(
            self.context, confirmed_force=confirmed_force
        ), done)

    @Slot()
    def reload(self) -> None:
        if not hasattr(self, "_local_jobs"):
            self._accounts = self.context.repository.list_accounts()
            self._refresh_navigation()
            if getattr(self, "_keyword_query", ""):
                self._reload_keyword_records()
            else:
                self._threads = self.context.repository.list_threads(
                    self._selected_account or None, mailbox=self._selected_mailbox,
                    category=self._selected_category)
            self.accountsChanged.emit()
            self.threadsChanged.emit()
            self.navigationChanged.emit()
            return
        token = uuid.uuid4().hex
        self._view_token = token
        scope, mailbox, category = self._selected_account, self._selected_mailbox, self._selected_category
        query, ids = self._keyword_query, list(self._keyword_thread_ids)
        def work():
            if self._view_token != token:
                return None
            repo = self.context.repository
            accounts = repo.list_accounts()
            counts, unread = repo.mailbox_counts(scope or None), repo.unread_counts(scope or None)
            if query:
                rows = repo.list_threads(scope or None, mailbox="all", limit=len(ids), thread_ids=ids)
                by_id = {row["id"]: row for row in rows}
                rows = [by_id[item] for item in ids if item in by_id]
            else:
                rows = repo.list_threads(scope or None, mailbox=mailbox, category=category)
            pending_index = repo.db.scalar("SELECT count(*) FROM enrichment_jobs", default=0)
            drafts_checked = repo.db.scalar("SELECT value FROM app_settings WHERE key=?",
                                           (APPLE_MAIL_DRAFT_REFRESH_KEY,), "")
            configured = repo.db.scalar("SELECT value FROM app_settings WHERE key='openai_api_key_configured'", default="0") == "1"
            backend = getattr(self, "_active_backend", None) or self.context.openai.backend_name()
            return accounts, counts, unread, rows, repo.pending_original_count(), pending_index, drafts_checked, configured, backend
        def done(result):
            if self._view_token != token or result is None:
                return
            accounts, counts, unread, rows, pending, pending_index, drafts_checked, configured, backend = result
            self._accounts, self._threads = accounts, rows
            self._accounts_loaded = True
            self._pending_original_count = pending
            self._indexing_pending = pending_index
            self._api_configured, self._active_backend = configured, backend
            if drafts_checked:
                self._drafts_last_checked = drafts_checked
            for row in rows:
                cached = self._read_cache.get(row["id"])
                if cached and cached[0].get("content_hash") != row.get("content_hash"):
                    self._read_cache_bytes -= self._read_cache.pop(row["id"])[1]
            self._apply_navigation(counts, unread)
            self.accountsChanged.emit()
            self.threadsChanged.emit()
            self.navigationChanged.emit()
            self.settingsChanged.emit()
            self.statusChanged.emit()
        self._run_local(work, done)

    def _refresh_navigation(self) -> None:
        if hasattr(self, "_local_jobs"):
            self.reload()
            return
        counts = self.context.repository.mailbox_counts(self._selected_account or None)
        unread = self.context.repository.unread_counts(self._selected_account or None)
        self._apply_navigation(counts, unread)

    def _apply_navigation(self, counts: dict, unread: dict) -> None:
        self._mailboxes = [
            {
                "id": "inbox",
                "label": "Inbox",
                "icon": "▣",
                "count": counts["inbox"],
                "unread": unread["inbox"],
            },
            {
                "id": "important",
                "label": "Important",
                "icon": "☆",
                "count": counts["important"],
            },
            {"id": "sent", "label": "Sent", "icon": "➤", "count": counts["sent"]},
            {"id": "drafts", "label": "Drafts", "icon": "▤", "count": counts["drafts"]},
        ]
        self._categories = [
            {"id": "all", "label": "All", "count": counts["inbox"], "unread": unread["inbox"]},
            {
                "id": "primary",
                "label": "Primary",
                "icon": "▣",
                "count": counts["primary_count"],
                "unread": unread.get("primary", 0),
            },
            {
                "id": "updates",
                "label": "Updates",
                "icon": "ⓘ",
                "count": counts["updates_count"],
                "unread": unread.get("updates", 0),
            },
        ]
        if self._selected_mailbox == "inbox":
            selected = next(
                item for item in self._categories if item["id"] == self._selected_category
            )
        else:
            selected = next(
                item for item in self._mailboxes if item["id"] == self._selected_mailbox
            )
        self._view_title = selected["label"]
        self._view_count = selected["count"]
        if getattr(self, "_keyword_query", ""):
            self._view_title = "Search results"
            self._view_count = len(self._threads)

    def _clear_keyword_search(self) -> None:
        self._keyword_query = ""
        self._keyword_thread_ids = []
        self._keyword_token = str(uuid.uuid4())

    def _reload_keyword_records(self) -> None:
        records = {item["id"]: item for item in self.context.repository.list_threads(
            self._selected_account or None, limit=len(self._keyword_thread_ids),
            mailbox="all", thread_ids=self._keyword_thread_ids)}
        self._threads = [records[item] for item in self._keyword_thread_ids if item in records]
        self._view_title = "Search results"
        self._view_count = len(self._threads)

    @Slot(str)
    def filterAccount(self, account_id: str) -> None:
        self._source_link_token = ""
        changed = self._selected_account != account_id
        self._clear_keyword_search()
        self._selected_account = account_id
        if changed:
            self._read_token = ""
            self._selected_thread = {}
            self._focused_message_id = ""
            self.sourceFocusChanged.emit()
            self.selectedThreadChanged.emit()
            self.closeSearchSource()
        self.reload()
        self.threadsChanged.emit()
        self.navigationChanged.emit()
        self.chatChanged.emit()

    @Slot(str)
    def filterMailbox(self, mailbox: str) -> None:
        self._source_link_token = ""
        if mailbox not in {"inbox", "important", "sent", "drafts"}:
            return
        self._read_token = ""
        self._clear_keyword_search()
        self._selected_mailbox = mailbox
        if mailbox == "inbox" and self._selected_category not in {"all", "primary", "updates"}:
            self._selected_category = "primary"
        self._selected_thread = {}
        self.reload()
        self.selectedThreadChanged.emit()
        self.threadsChanged.emit()
        self.navigationChanged.emit()

    @Slot(str)
    def filterCategory(self, category: str) -> None:
        if category not in {"all", "primary", "updates"}:
            return
        self._read_token = ""
        self._clear_keyword_search()
        self._selected_mailbox = "inbox"
        self._selected_category = category
        self._selected_thread = {}
        self.reload()
        self.selectedThreadChanged.emit()
        self.threadsChanged.emit()
        self.navigationChanged.emit()

    @Slot(str)
    def selectThread(self, thread_id: str) -> None:
        if hasattr(self, "_local_jobs"):
            self.selectThreadAsync(thread_id)
            return
        if hasattr(self, "_read_token"):
            self._read_token = ""
        # A few local diagnostics construct the controller without the normal
        # initializer; keep rendering caches lazy for those read-only paths.
        if not hasattr(self, "_message_remote_images"):
            self._message_remote_images = {}
        if not hasattr(self, "_message_data_urls"):
            self._message_data_urls = {}
        self._update_read_state(thread_id, False)
        self._selected_thread = self.context.repository.get_thread(thread_id) or {}
        self._focused_message_id = ""
        self.sourceFocusChanged.emit()
        self.mail_images.digests.clear()
        self.mail_images.remote.clear()
        self.mail_images.decoded.clear()
        for message in self._selected_thread.get("messages", []):
            self._render_message(message)
        # Repository keeps chronological order for AI context; only presentation is reversed.
        self._selected_thread.get("messages", []).reverse()
        self._assistant = {}
        self.selectedThreadChanged.emit()
        self.assistantChanged.emit()

    @Slot(str)
    def selectThreadAsync(self, thread_id: str) -> None:
        """Show the selection immediately, then prepare local mail off the UI thread."""
        self._source_link_token = ""
        row = next((item for item in self._threads if item["id"] == thread_id), None)
        if row is None:
            row = {"id": thread_id, "subject": "Opening message…"}
        self._focused_message_id = ""
        self.sourceFocusChanged.emit()
        self._assistant = {}
        self.assistantChanged.emit()
        self._selected_thread = {**row, "messages": [], "read_state": "loading"}
        self.selectedThreadChanged.emit()
        if row.get("is_unread"):
            row["is_unread"] = 0
            self.threadsChanged.emit()

        cached = self._read_cache.get(thread_id)
        if cached is not None:
            self._read_cache.move_to_end(thread_id)
            self._selected_thread = {**cached[0], "is_unread": 0, "read_state": "ready"}
            self.selectedThreadChanged.emit()

        def prepare() -> dict[str, Any] | None:
            if self._read_token != worker.token:
                return None  # Skip queued reads superseded by a later click.
            self.context.repository.set_thread_unread(thread_id, False)
            if cached is not None:
                current_hash = self.context.database.scalar(
                    "SELECT content_hash FROM threads WHERE id=?", (thread_id,), None)
                if current_hash == cached[0].get("content_hash"):
                    return None
            thread = self.context.repository.get_thread_for_read(thread_id)
            if thread is None:
                return None
            for index, message in enumerate(thread["messages"]):
                message["prepared"] = index == len(thread["messages"]) - 1
                if message["prepared"]:
                    self._render_message(message, register_images=False)
                    message["_default_document"] = email_document(
                        message["display_html"],
                        images=self._document_images(message),
                    )
            thread["messages"].reverse()
            thread["read_state"] = "ready"
            return thread

        worker = Worker(prepare)
        self._read_token = worker.token
        self._read_jobs[worker.token] = worker
        worker.signals.succeeded.connect(self._read_succeeded)
        worker.signals.failed.connect(self._read_failed)
        self.read_pool.start(worker)

    @Slot(str, object)
    def _read_succeeded(self, worker_token: str, result: object) -> None:
        self._read_jobs.pop(worker_token, None)
        if worker_token != self._read_token:
            return
        if isinstance(result, dict):
            for message in result.get("messages", []):
                self.mail_images.register(message["attachments"])
            self._selected_thread = result
            self.selectedThreadChanged.emit()
            size = sum(
                len(str(message.get(key) or "")) * 4
                for message in result.get("messages", [])
                for key in ("body_html", "body_text", "display_html", "original_html", "_default_document")
            )
            if size <= 96 * 1024 * 1024:
                previous = self._read_cache.pop(result["id"], None)
                if previous is not None:
                    self._read_cache_bytes -= previous[1]
                self._read_cache[result["id"]] = (result, size)
                self._read_cache.move_to_end(result["id"])
                self._read_cache_bytes += size
                while self._read_cache_bytes > 96 * 1024 * 1024:
                    _, (_, removed_size) = self._read_cache.popitem(last=False)
                    self._read_cache_bytes -= removed_size
            self._trim_render_caches()
        focus = getattr(self, "_focus_after_read", None)
        if focus and self._selected_thread.get("id") == focus[0]:
            self._focused_message_id = focus[1]
            self._focus_after_read = None
            self.prepareMessage(focus[1])
            self.sourceFocusChanged.emit()
        self._refresh_navigation()
        self.navigationChanged.emit()

    @Slot(str, str)
    def _read_failed(self, worker_token: str, _error: str) -> None:
        self._read_jobs.pop(worker_token, None)
        if worker_token != self._read_token:
            return
        self._selected_thread = {**self._selected_thread, "read_state": "error"}
        self.selectedThreadChanged.emit()
        self._set_status("Could not open this saved email; choose it again to retry")

    @Slot(str)
    def prepareMessage(self, message_id: str) -> None:
        """Prepare one expanded older message without blocking scrolling."""
        thread_id = self._selected_thread.get("id", "")
        message = next(
            (item for item in self._selected_thread.get("messages", []) if item["id"] == message_id),
            None,
        )
        if not message or message.get("prepared"):
            return
        if any(job[1:] == (thread_id, message_id)
               and getattr(job[0], "read_generation", None) == self._read_token
               for job in self._message_jobs.values()):
            return
        message["preparing"] = True
        self.selectedThreadChanged.emit()

        def prepare() -> dict[str, Any]:
            if self._selected_thread.get("id") != thread_id or self._closing:
                return {}
            prepared = self.context.repository.prepare_message_for_read(message_id) or dict(message)
            self._render_message(prepared, register_images=False)
            prepared["_default_document"] = email_document(
                prepared["display_html"],
                images=self._document_images(prepared),
            )
            prepared["prepared"] = True
            prepared["preparing"] = False
            return prepared

        worker = Worker(prepare)
        worker.read_generation = self._read_token
        self._message_jobs[worker.token] = (worker, thread_id, message_id)
        worker.signals.succeeded.connect(self._message_succeeded)
        worker.signals.failed.connect(self._message_failed)
        self.read_pool.start(worker)

    @Slot(str, object)
    def _message_succeeded(self, token: str, result: object) -> None:
        job = self._message_jobs.pop(token, None)
        if (not job or not isinstance(result, dict) or result.get("id") != job[2]
                or self._selected_thread.get("id") != job[1] or self._closing
                or getattr(job[0], "read_generation", None) != self._read_token):
            return
        for index, item in enumerate(self._selected_thread.get("messages", [])):
            if item["id"] == job[2]:
                self._selected_thread["messages"][index] = result
                self.mail_images.register(result["attachments"])
                self._read_cache.pop(job[1], None)
                self._read_cache_bytes = sum(size for _, size in self._read_cache.values())
                self.selectedThreadChanged.emit()
                break

    @Slot(str, str)
    def _message_failed(self, token: str, _error: str) -> None:
        job = self._message_jobs.pop(token, None)
        if job and self._selected_thread.get("id") == job[1]:
            for item in self._selected_thread.get("messages", []):
                if item["id"] == job[2]:
                    item["preparing"] = False
            self.selectedThreadChanged.emit()
            self._set_status("Could not open this message; try again")

    @Slot(str, int, result=str)
    def fitMessageImages(self, markup: str, width: int) -> str:
        return fit_message_images(markup, width)

    @Slot(str, bool, bool, bool, result=str)
    def messageDocument(
        self,
        message_id: str,
        show_original: bool,
        reading_mode: bool,
        highlight_details: bool,
    ) -> str:
        message = next(
            (
                item
                for item in self._selected_thread.get("messages", [])
                if item["id"] == message_id
            ),
            None,
        )
        return self._message_document(message, show_original, reading_mode, highlight_details)

    def _message_document(self, message: dict | None, show_original: bool,
                          reading_mode: bool, highlight_details: bool) -> str:
        if message is None or not message.get("prepared", True):
            return email_document("")
        if not show_original and not reading_mode and not highlight_details and message.get("_default_document"):
            return message["_default_document"]
        message_id = message["id"]
        revision = (message.get("content_fingerprint", ""), message.get("_document_revision", ""),
                    tuple((item.get("content_digest"), item.get("content_id"), item.get("content_location"))
                          for item in message.get("attachments", [])))
        key = (message_id, revision, show_original, reading_mode, highlight_details)
        if message.get("_active_document_key") == key:
            return message["_active_document"]
        fragment = message["original_html" if show_original else "display_html"]
        # Minimal standalone controller probes retain the synchronous pure-render path.
        if not hasattr(self, "_local_jobs"):
            images = dict(self._message_data_urls.get(message_id, {}))
            images.update(image_data_urls(message.get("attachments", []), self.context.repository.blobs))
            return email_document(fragment, images=images, reading_mode=reading_mode,
                                  highlight_details=highlight_details)
        if key in self._document_cache:
            self._document_cache.move_to_end(key)
            return self._document_cache[key]
        if key not in self._document_jobs:
            self._document_jobs.add(key)
            snapshot = dict(message)
            remote = dict(self._message_data_urls.get(message_id, {}))
            def work():
                visible_ids = {item["id"] for item in self._selected_thread.get("messages", [])}
                visible_ids.update(item["id"] for item in self._source_preview.get("messages", []))
                if message_id not in visible_ids or self._closing:
                    return None
                images = dict(remote)
                images.update(image_data_urls(snapshot.get("attachments", []), self.context.repository.blobs))
                return email_document(fragment, images=images, reading_mode=reading_mode,
                                      highlight_details=highlight_details)
            def done(document):
                self._document_jobs.discard(key)
                if document is not None:
                    if len(document) * 4 <= 96 * 1024 * 1024:
                        self._document_cache[key] = document
                    else:
                        message["_active_document_key"] = key
                        message["_active_document"] = document
                    self._trim_render_caches()
                    self.documentReady.emit(message_id)
            self._run_local(work, done, failed=lambda: self._document_jobs.discard(key), pool=self.read_pool)
        return message.get("_default_document") or email_document("Preparing this display…")

    def _trim_render_caches(self) -> None:
        # Account for Python Unicode storage conservatively; originals stay encrypted on disk.
        limit = 96 * 1024 * 1024
        def used():
            return self._read_cache_bytes + sum(len(v) * 4 for v in self._document_cache.values()) + sum(
                len(value) * 4 for images in self._message_data_urls.values() for value in images.values())
        while self._read_cache and used() > limit:
            _, (_, size) = self._read_cache.popitem(last=False)
            self._read_cache_bytes -= size
        while self._document_cache and used() > limit:
            self._document_cache.popitem(last=False)
        while self._message_data_urls and used() > limit:
            identifier = next(iter(self._message_data_urls))
            self._message_data_urls.pop(identifier, None)
            self._message_remote_images.pop(identifier, None)
        active = {item["id"] for item in self._selected_thread.get("messages", [])}
        active.update(item["id"] for item in self._source_preview.get("messages", []))
        for identifier in list(self._message_data_urls):
            if identifier not in active:
                self._message_data_urls.pop(identifier, None)
                self._message_remote_images.pop(identifier, None)

    @Slot(str)
    def openSearchSource(self, link: str) -> None:
        url = QUrl(link)
        if url.scheme() != "emailreader" or url.host() != "source":
            return
        identifier = url.path().strip("/")
        if not identifier:
            return
        self._source_preview = {"read_state": "loading", "source_id": identifier, "messages": []}
        self.sourcePreviewChanged.emit()

        def prepare() -> dict[str, Any] | None:
            if self._source_token != worker.token or self._closing:
                return None
            repository = self.context.repository
            resolved = repository.resolve_source_link(identifier)
            if resolved is None:
                return None
            thread_id, message_id = resolved
            thread = repository.get_thread_for_read(thread_id)
            if not thread:
                return None
            message = repository.prepare_message_for_read(message_id)
            if message is None:
                return None
            self._render_message(message, register_images=False)
            message["_default_document"] = email_document(
                message["display_html"],
                images=self._document_images(message),
            )
            message["prepared"] = True
            citation = repository.citation_for_chunk(identifier)
            return {
                **thread,
                "read_state": "ready",
                "source_id": identifier,
                "message_id": message_id,
                "locator": citation.locator if citation else "",
                "citation": citation.model_dump(mode="json") if citation else {},
                "messages": [message],
            }

        worker = Worker(prepare)
        self._source_token = worker.token
        self._source_jobs[worker.token] = worker
        worker.signals.succeeded.connect(self._source_succeeded)
        worker.signals.failed.connect(self._source_failed)
        self.read_pool.start(worker)

    @Slot(str, object)
    def _source_succeeded(self, token: str, result: object) -> None:
        self._source_jobs.pop(token, None)
        if token != self._source_token:
            return
        if not isinstance(result, dict):
            self._source_failed(token, "")
            return
        for message in result["messages"]:
            self.mail_images.register(message["attachments"])
        self._source_preview = result
        self.sourcePreviewChanged.emit()

    @Slot(str, str)
    def _source_failed(self, token: str, _error: str) -> None:
        self._source_jobs.pop(token, None)
        if token != self._source_token:
            return
        self._source_preview = {
            **self._source_preview,
            "read_state": "error",
            "error": "This source is unavailable. Try another citation or restore it from Trash.",
        }
        self.sourcePreviewChanged.emit()

    @Slot()
    def closeSearchSource(self) -> None:
        self._source_token = ""
        self._source_preview = {}
        self.sourcePreviewChanged.emit()

    @Slot(str, bool, bool, bool, result=str)
    def sourceMessageDocument(
        self,
        message_id: str,
        show_original: bool,
        reading_mode: bool,
        highlight_details: bool,
    ) -> str:
        message = next(
            (item for item in self._source_preview.get("messages", []) if item["id"] == message_id),
            None,
        )
        return self._message_document(message, show_original, reading_mode, highlight_details)

    @Slot(str, result=str)
    @Slot(str, "QVariantList", result=str)
    def assistantTextHtml(self, text: str, citations: list | None = None) -> str:
        # Treat AI output as plain text. Only our safe link renderer may add
        # markup; model-supplied HTML, scripts, and images are never executed.
        if citations is not None:
            from email_reader.rendering import render_assistant_text
            return render_assistant_text(text, citations)
        return render_message(text, "", []).html

    @Slot(str, result=str)
    def assistantExcerptHtml(self, text: str) -> str:
        return render_source_excerpt(text)

    @Slot(str, "QVariantList", result=str)
    def answerTextHtml(self, text: str, citations: list) -> str:
        from email_reader.rendering import render_assistant_text

        return render_assistant_text(ensure_answer_citations(text, citations), citations)

    def _document_images(self, message: dict) -> dict:
        images = dict(self._message_data_urls.get(message["id"], {}))
        images.update(image_data_urls(message.get("attachments", []), self.context.repository.blobs))
        return images

    def _render_message(self, message: dict, *, register_images: bool = True, remote_images=None) -> None:
        message.pop("_default_document", None)
        attachments = message["attachments"]
        if register_images:
            self.mail_images.register(attachments)
        remote = remote_images if remote_images is not None else self._message_remote_images.get(message["id"], {})
        full = render_message(
            message["body_text"], message["body_html"], attachments, remote_images=remote
        )
        collapsed_text = ""
        for candidate in (
            message.get("new_content", ""),
            message.get("body_text", ""),
            message.get("snippet", ""),
        ):
            if candidate.translate({ord("\u200b"): None, ord("\ufeff"): None}).strip():
                collapsed_text = candidate
                break
        collapsed = full if message["is_draft"] else render_message(
            collapsed_text,
            message["body_html"],
            attachments,
            quoted=False,
            remote_images=remote,
        )
        message["display_html"] = collapsed.html
        message["original_html"] = full.html
        message["remote_image_count"] = len(full.remote_urls - remote.keys())
        message.setdefault(
            "remote_image_state",
            "loaded" if full.remote_urls and not message["remote_image_count"] else (
                "blocked" if message["remote_image_count"] else "none"
            ),
        )
        message.setdefault("remote_image_completed", len(full.remote_urls & remote.keys()))
        message.setdefault("remote_image_total", len(full.remote_urls))
        embedded = full.embedded_ids | collapsed.embedded_ids
        inline_images = {
            item["id"] for item in attachments
            if is_mail_image(item.get("mime_type", ""), item.get("filename", ""))
            and (item["id"] in embedded or item["is_inline"])
        }
        inline_digests = {
            item.get("content_digest") for item in attachments
            if item["id"] in inline_images and item.get("content_digest")
        }
        # If a mail client exposes the same decorative/signature image twice,
        # keep both originals accessible but do not advertise the duplicate as
        # a separate user attachment.
        inline_images.update(
            item["id"] for item in attachments
            if is_mail_image(item.get("mime_type", ""), item.get("filename", ""))
            and item.get("content_digest") in inline_digests
        )
        # Detect actual 1×1 tracking payloads by decoded dimensions, never by
        # filename. They remain available under embedded image originals.
        for item in attachments:
            if (
                item["id"] in inline_images
                or not item.get("content_digest")
                or int(item.get("byte_size") or 0) > 4096
                or not is_mail_image(item.get("mime_type", ""), item.get("filename", ""))
            ):
                continue
            try:
                probe = QImage.fromData(self.context.repository.blobs.get(item["content_digest"]))
                if probe.width() == 1 and probe.height() == 1:
                    inline_images.add(item["id"])
            except Exception:
                pass
        message["file_attachments"] = [
            item for item in attachments if item["id"] not in inline_images
        ]
        message["inline_attachments"] = [
            item for item in attachments if item["id"] in inline_images
        ]

    @Slot(str)
    def loadRemoteImages(self, message_id: str) -> None:
        message = next(
            (
                item
                for item in self._selected_thread.get("messages", [])
                if item["id"] == message_id
            ),
            None,
        )
        if message is None:
            return
        if any(job["message_id"] == message_id for job in self._image_jobs.values()):
            self._set_status("Those images are already loading")
            return
        thread_id = self._selected_thread["id"]
        urls = render_message(
            message["body_text"], message["body_html"], message["attachments"]
        ).remote_urls
        urls -= self._message_remote_images.get(message_id, {}).keys()

        targets = sorted(urls)[:30]
        if not targets:
            return
        cancel = threading.Event()
        message.update(
            remote_image_state="loading",
            remote_image_completed=0,
            remote_image_total=len(targets),
        )
        self.selectedThreadChanged.emit()

        def work(report):
            return fetch_remote_images(targets, report=report, cancel=cancel)

        worker = Worker(work, reports_progress=True)
        self._image_jobs[worker.token] = {
            "worker": worker,
            "message_id": message_id,
            "thread_id": thread_id,
            "cancel": cancel,
            "targets": targets,
            "message": message,
        }
        worker.signals.progressed.connect(self._image_progressed)
        worker.signals.succeeded.connect(self._image_succeeded)
        worker.signals.failed.connect(self._image_failed)
        self.image_pool.start(worker)

    @Slot(str)
    def cancelRemoteImages(self, message_id: str) -> None:
        for job in self._image_jobs.values():
            if job["message_id"] == message_id:
                job["cancel"].set()
                self._set_status("Cancelling remote image loading…")
                return

    @Slot(str, object)
    def _image_progressed(self, token: str, update: object) -> None:
        job = self._image_jobs.get(token)
        if not job or not isinstance(update, dict):
            return
        message = self._find_selected_message(job["message_id"])
        if message and message is job.get("message", message):
            message["remote_image_completed"] = int(update.get("completed", 0))
            message["remote_image_total"] = int(update.get("total", 0))
            self.selectedThreadChanged.emit()

    def _find_selected_message(self, message_id: str) -> dict | None:
        return next(
            (
                item for item in self._selected_thread.get("messages", [])
                if item.get("id") == message_id
            ),
            None,
        )

    @Slot(str, object)
    def _image_succeeded(self, token: str, result: object) -> None:
        job = self._image_jobs.pop(token, None)
        if not job or not isinstance(result, dict):
            return
        message_id = job["message_id"]
        message = self._find_selected_message(message_id)
        if not message or message is not job.get("message", message) or self._closing:
            return
        snapshot = dict(message)
        old_mapping = dict(self._message_remote_images.get(message_id, {}))
        old_urls = dict(self._message_data_urls.get(message_id, {}))
        def prepare():
            # Always retain CID originals when adding explicitly requested remote pictures.
            urls = dict(old_urls)
            urls.update(image_data_urls(snapshot.get("attachments", []), self.context.repository.blobs))
            mapping = old_mapping
            decoded = {}
            for url, content in result.get("loaded", {}).items():
                value = QImage.fromData(content)
                if value.isNull():
                    continue
                identifier = uuid.uuid4().hex
                decoded[identifier] = value
                mapping[url] = identifier
                urls[identifier] = f"data:{raster_mime(content)};base64,{b64encode(content).decode('ascii')}"
            self._render_message(snapshot, register_images=False, remote_images=mapping)
            snapshot["_document_revision"] = uuid.uuid4().hex
            snapshot["_default_document"] = email_document(snapshot["display_html"], images=urls)
            remaining = snapshot.get("remote_image_count", 0)
            snapshot["remote_image_state"] = "cancelled" if result.get("cancelled") else "failed" if remaining else "loaded"
            snapshot["remote_image_completed"] = len(decoded)
            snapshot["remote_image_total"] = len(job["targets"])
            return snapshot, mapping, urls, decoded
        def done(prepared):
            current, mapping, urls, decoded = prepared
            if (self._selected_thread.get("id") != job["thread_id"]
                    or self._find_selected_message(message_id) is not message or self._closing):
                return
            self._message_remote_images[message_id] = mapping
            self._message_data_urls[message_id] = urls
            for identifier, value in decoded.items():
                self.mail_images.remote[identifier] = value
            for index, item in enumerate(self._selected_thread.get("messages", [])):
                if item["id"] == message_id:
                    self._selected_thread["messages"][index] = current
            previous = self._read_cache.pop(job["thread_id"], None)
            if previous:
                self._read_cache_bytes -= previous[1]
            self._trim_render_caches()
            self.selectedThreadChanged.emit()
            self.documentReady.emit(message_id)
            self._set_status("Requested pictures loaded; embedded originals preserved")
        if hasattr(self, "_local_jobs"):
            self._run_local(prepare, done, pool=self.read_pool)
        else:
            done(prepare())

    @Slot(str, str)
    def _image_failed(self, token: str, _error: str) -> None:
        job = self._image_jobs.pop(token, None)
        if not job:
            return
        message = self._find_selected_message(job["message_id"])
        if message and message is job.get("message", message):
            message["remote_image_state"] = "failed"
            self.selectedThreadChanged.emit()
        self._set_status("Remote images could not be loaded; you can retry")

    def _update_read_state(self, thread_id: str, unread: bool) -> None:
        if hasattr(self, "_local_jobs"):
            self._run_local(lambda: self.context.repository.set_thread_unread(thread_id, unread),
                            lambda _: self.reload())
        else:
            self.context.repository.set_thread_unread(thread_id, unread)
        for thread in self._threads:
            if thread["id"] == thread_id:
                thread["is_unread"] = int(unread)
        self._refresh_navigation()
        self.threadsChanged.emit()
        self.navigationChanged.emit()

    @Slot()
    def markUnread(self) -> None:
        if self._selected_thread.get("id"):
            self._update_read_state(self._selected_thread["id"], True)
            self._selected_thread["is_unread"] = 1
            self._set_status("Marked unread on this Mac; provider read status unchanged")

    @Slot(str)
    def search(self, query: str) -> None:
        query = query.strip()
        if not query:
            self._clear_keyword_search()
            self.reload()
            return
        token = str(uuid.uuid4())
        self._keyword_token = token
        account_id = self._selected_account

        def work(report):
            if self._keyword_token != token:
                return [], []
            report({"stage": "Searching the encrypted index", "detail": query})
            hits = self.context.search.search(
                query, account_id=account_id or None, limit=50, semantic=False
            )
            report({"stage": "Preparing results", "detail": f"{len(hits)} matches found"})
            thread_ids = self.context.repository.search_header_thread_ids(query, account_id or None)
            for hit in hits:
                thread_id = self.context.database.scalar(
                    "SELECT thread_id FROM source_chunks WHERE id=?", (hit.chunk_id,)
                )
                if thread_id and thread_id not in thread_ids:
                    thread_ids.append(thread_id)
            records = {item["id"]: item for item in self.context.repository.list_threads(
                account_id or None, limit=len(thread_ids), mailbox="all", thread_ids=thread_ids
            )}
            return thread_ids, [records[item] for item in thread_ids if item in records]

        def done(result):
            if self._keyword_token != token or self._selected_account != account_id:
                return
            self._view_token = ""
            thread_ids, records = result
            self._keyword_query = query
            self._keyword_thread_ids = thread_ids
            if self._selected_thread.get("id") and self._selected_thread["id"] not in thread_ids:
                self._read_token = ""
                self._selected_thread = {}
                self._focused_message_id = ""
                self.sourceFocusChanged.emit()
                self.selectedThreadChanged.emit()
            self._threads = records
            self._view_title = "Search results"
            self._view_count = len(records)
            self.threadsChanged.emit()
            self.navigationChanged.emit()

        self._run_keyword(work, done, token)

    def _run_keyword(self, work, done, token: str) -> None:
        """Local search must not wait behind Apple Mail, OCR, or AI jobs."""
        if not self._busy:
            self._set_status("Searching saved mail on this Mac…")
        worker = Worker(lambda: work(lambda update: None))
        self._keyword_jobs[worker.token] = {"worker": worker, "done": done, "token": token}
        worker.signals.succeeded.connect(self._keyword_succeeded)
        worker.signals.failed.connect(self._keyword_failed)
        self.keyword_pool.start(worker)

    @Slot(str, object)
    def _keyword_succeeded(self, worker_token: str, result: object) -> None:
        job = self._keyword_jobs.pop(worker_token, None)
        if not job or job["token"] != self._keyword_token:
            return
        job["done"](result)
        if not self._busy:
            self._set_status(f"{len(self._threads)} conversations found on this Mac")

    @Slot(str, str)
    def _keyword_failed(self, worker_token: str, error: str) -> None:
        job = self._keyword_jobs.pop(worker_token, None)
        if job and job["token"] == self._keyword_token:
            self._set_status("Local search could not finish; please try again")

    @Slot()
    def summarize(self) -> None:
        thread_id = self._selected_thread.get("id")
        if not thread_id or self.aiBusy:
            return
        def done(payload):
            if self._selected_thread.get("id") != thread_id:
                return
            self._assistant = payload
            self.assistantChanged.emit()
        def work(report):
            report({"stage": "Reading the conversation", "detail": "Collecting cited context"})
            summary = self.context.assistant.summarize_thread(thread_id)
            thread = self.context.repository.get_thread(thread_id) or {}
            return {**audited_summary(summary.model_dump(mode="json"), thread), "kind": "summary"}
        self._run("Creating summary…", work, done, reports_progress=True, ai=True)

    @Slot(str)
    def ask(self, question: str) -> None:
        question = question.strip()
        if not question or self.aiBusy:
            return
        scope = self._selected_account
        session = self._search_session()
        history = session["messages"]
        if not history:
            session["title"] = question[:80]
        previous = [{"role": item["role"], "text": item["text"]} for item in history[-6:]]
        history.append({"role": "user", "text": question, "citations": [], "attachments": []})
        version = session["version"]
        self._pending_search = session
        session["preview"] = {}
        self.chatChanged.emit()
        def local_preview():
            hits = self.context.search.search(question, account_id=scope or None, limit=3, semantic=False)
            identifiers = []
            for hit in hits:
                rows = self.context.database.query("SELECT source_type,source_id FROM source_chunks WHERE id=?", (hit.chunk_id,))
                if rows and rows[0]["source_type"] == "attachment":
                    identifiers.append(rows[0]["source_id"])
            return {"citations": [hit.citation.model_dump(mode="json") for hit in hits],
                    "file_cards": self.context.repository.answer_file_cards([], identifiers, question=question),
                    "pending_indexing": self.indexingPending}
        def preview_done(payload):
            if self._pending_search is session and session["version"] == version:
                session["preview"] = payload
                self.chatChanged.emit()
        if hasattr(self, "_local_jobs"):
            self._run_local(local_preview, preview_done, failed=lambda: None)
        def work(report):
            report({"stage": "Searching messages and files", "detail": "Checking saved evidence"})
            answer, hits = self.context.assistant.answer(question, scope or None, previous)
            self.context.openai._check_cancelled()
            hit_map = {hit.chunk_id: hit for hit in hits}
            def citation_payload(identifier):
                payload = hit_map[identifier].citation.model_dump(mode="json")
                resolved = self.context.repository.resolve_source_link(identifier)
                if resolved:
                    payload["thread_id"], payload["message_id"] = resolved
                return payload
            files = self.context.repository.answer_file_cards(
                answer.attachment_ids, answer.related_attachment_ids, question=question)
            return {
                **answer.model_dump(mode="json"), "kind": "answer", "role": "assistant",
                "citations": [citation_payload(i) for i in answer.source_ids if i in hit_map],
                "related_citations": [citation_payload(i) for i in answer.related_source_ids if i in hit_map],
                "attachments": [card for card in files if not card["possible_match"]],
                "related_attachments": [card for card in files if card["possible_match"]],
                "file_cards": files,
            }
        def done(payload):
            if self._pending_search is session:
                self._pending_search = None
            session["preview"] = {}
            if session["version"] != version:
                return
            history.append(payload)
            if self._selected_account == scope and self.selectedSearchId == session["id"]:
                self.chatChanged.emit()
        self._run("Searching your email…", work, done, reports_progress=True, ai=True)

    @Slot(str, bool)
    def setImportant(self, thread_id: str, value: bool) -> None:
        if self._selected_thread.get("id") == thread_id:
            self._selected_thread["important_override"] = int(value)
            self.selectedThreadChanged.emit()
        self._run_local(lambda: self.context.repository.set_thread_important(thread_id, value),
                        lambda _: self.reload())

    @Slot(str)
    def openAttachment(self, attachment_id: str) -> None:
        if not hasattr(self, "_attachment_jobs"):
            self._attachment_jobs = set()
        if attachment_id in self._attachment_jobs:
            return
        self._attachment_jobs.add(attachment_id)
        self._set_status("Preparing original file…")
        def work():
            folder = self.context.runtime_dir / hashlib.sha256(attachment_id.encode()).hexdigest()
            return self.context.repository.export_attachment(attachment_id, folder)
        def done(path):
            self._attachment_jobs.discard(attachment_id)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            self._set_status("Opened the original file")
        def failed():
            self._attachment_jobs.discard(attachment_id)
            self._set_status("Could not open this original file; please retry")
        self._run_local(work, done, failed=failed)

    @Slot(str)
    def showAttachmentPreview(self, attachment_id: str) -> None:
        attachment = next(
            (
                item
                for message in (
                    self._selected_thread.get("messages", [])
                    + self._source_preview.get("messages", [])
                )
                for item in message.get("attachments", [])
                if item.get("id") == attachment_id
            ),
            None,
        )
        if not attachment or not attachment.get("content_digest") or not is_mail_image(
            attachment.get("mime_type", ""), attachment.get("filename", "")
        ):
            self._set_status("This image original is unavailable")
            return
        self.mail_images.register([attachment])
        self._preview_attachment = {
            "id": attachment_id,
            "filename": attachment.get("filename") or "Image",
            "source": f"image://mail/{attachment_id}",
        }
        self.previewAttachmentChanged.emit()

    @Slot()
    def closeAttachmentPreview(self) -> None:
        self._preview_attachment = {}
        self.previewAttachmentChanged.emit()

    @Slot(str)
    def openLink(self, link: str) -> None:
        if link.startswith("emailreader://preview/"):
            self.showAttachmentPreview(QUrl(link).path().strip("/"))
            return
        if link.startswith("emailreader://source/"):
            identifier = QUrl(link).path().strip("/")
            token = uuid.uuid4().hex
            self._source_link_token = token
            def done(resolved):
                if resolved and self._source_link_token == token:
                    thread_id, message_id = resolved
                    self.selectThreadAsync(thread_id)
                    self._focus_after_read = (thread_id, message_id)
            self._run_local(lambda: self.context.repository.resolve_source_link(identifier), done)
            return
        if safe_link(link) or link.startswith("message://"):
            QDesktopServices.openUrl(QUrl(link))

    @Slot(str, str)
    def exportAttachment(self, attachment_id: str, destination_url: str) -> None:
        if not hasattr(self, "_attachment_jobs"):
            self._attachment_jobs = set()
        destination = QUrl(destination_url)
        if not destination.isLocalFile():
            self._set_status("Choose a local destination for this export")
            return
        if attachment_id in self._attachment_jobs:
            return
        self._attachment_jobs.add(attachment_id)
        path = Path(destination.toLocalFile())
        self._set_status("Exporting the selected original…")
        def work():
            row = self.context.repository.exportable_attachment(attachment_id)
            if not row or not row["content_digest"]:
                raise ValueError("Original unavailable")
            content = self.context.repository.blobs.get(row["content_digest"])
            if getattr(self, "_closing", False):
                return False
            output = QSaveFile(str(path))
            output.setDirectWriteFallback(False)
            if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError("Cannot open export destination")
            output.setPermissions(QFileDevice.Permission.ReadOwner | QFileDevice.Permission.WriteOwner)
            if output.write(content) != len(content) or getattr(self, "_closing", False):
                output.cancelWriting()
                raise OSError("Export cancelled")
            if not output.commit():
                raise OSError("Cannot commit export")
            return True
        def done(exported):
            self._attachment_jobs.discard(attachment_id)
            self._set_status("Exported the original file you selected" if exported else "Export cancelled")
        def failed():
            self._attachment_jobs.discard(attachment_id)
            self._set_status("This original is not available or could not be exported; the encrypted copy is unchanged")
        self._run_local(work, done, failed=failed)

    @Slot(str, result=str)
    def defaultExportUrl(self, filename: str) -> str:
        return QUrl.fromLocalFile(str(self.context.paths.exports / Path(filename).name)).toString()

    @Slot(str, str, str, str, str, str, str, float)
    def saveSetup(
        self,
        openai_key: str,
        ai_backend: str,
        google_client_id: str,
        google_client_secret: str,
        microsoft_client_id: str,
        microsoft_tenant: str,
        response_model: str,
        monthly_limit: float,
    ) -> None:
        pairs = {
            "openai-api-key": openai_key,
            "google-client-id": google_client_id,
            "google-client-secret": google_client_secret,
            "microsoft-client-id": microsoft_client_id,
            "microsoft-tenant": microsoft_tenant or "common",
        }
        if self.aiBusy or self.providerBusy or self._ai_enrichment_running:
            self._set_status("Pause import and finish the active AI request before changing connection settings")
            return
        def work():
            for key, value in pairs.items():
                if value.strip():
                    self.context.vault.set(key, value.strip())
                    if key == "openai-api-key":
                        self.context.save_setting("openai_api_key_configured", "1")
            if ai_backend in {"auto", "codex", "api"}:
                self.context.save_setting("ai_backend", ai_backend)
            if response_model.strip():
                self.context.save_setting("response_model", response_model.strip())
            self.context.save_setting("monthly_limit_usd", str(max(0.0, monthly_limit)))
            self.context.openai.usage.monthly_limit_usd = max(0.0, monthly_limit)
            return self.context.openai.backend_name()
        def done(backend):
            self._active_backend = backend
            self.reload()
            self.settingsChanged.emit()
            self._set_status("Settings saved securely")
        self._run("Saving settings…", work, done)

    @Slot(str, str, str)
    def connectAccount(self, provider_name: str, range_name: str, custom_date: str) -> None:
        if self.providerBusy:
            return
        account_id = str(uuid.uuid4())
        provider = (GmailProvider(account_id, self.context.vault) if provider_name == "gmail"
                    else OutlookProvider(account_id, self.context.vault))
        def work(report):
            report({"stage": "Opening provider sign-in", "detail": provider_name.title()})
            account = provider.authenticate()
            account.sync_from = self._sync_from(range_name, custom_date)
            self.context.repository.upsert_account(account)
            self._providers[account.id] = provider
            return self.context.repository.list_accounts()
        def done(accounts):
            self._accounts = accounts
            self.reload()
            self.syncAll()
        self._run(f"Connecting {provider_name.title()}…", work, done, reports_progress=True)

    @Slot(str, str)
    def importAppleMail(self, range_name: str, custom_date: str) -> None:
        if self.providerBusy:
            return
        sync_from = self._sync_from(range_name, custom_date)
        def work(report):
            report({"stage": "Discovering Apple Mail accounts", "detail": "No email will be sent"})
            infos = discover_apple_mail_accounts()
            if not infos:
                raise RuntimeError("No enabled accounts were found in Apple Mail")
            for info in infos:
                provider = AppleMailProvider(info.account_id, info)
                account = provider.authenticate()
                previous = self.context.database.query("SELECT * FROM accounts WHERE id=?", (account.id,))
                # Reconnecting preserves the original historical boundary and checkpoints.
                account.sync_from = (datetime.fromisoformat(previous[0]["sync_from"])
                    if previous and previous[0]["sync_from"] else None) if previous else sync_from
                self.context.repository.upsert_account(account)
                self._providers[account.id] = provider
            return self.context.repository.list_accounts()
        def done(accounts):
            self._accounts = accounts
            self.accountsChanged.emit()
            self.reload()
            self.syncAll()
        self._run("Connecting Apple Mail accounts…", work, done, reports_progress=True)

    def _bootstrap_apple_mail_drafts(
        self,
        account_id: str,
        provider: AppleMailProvider,
        since: datetime | None,
        report: Callable[[object], None],
        account_index: int,
        account_total: int,
    ) -> int:
        token = provider.draft_page_token()
        if not token:
            return 0
        self.context.database.execute(
            "UPDATE accounts SET sync_page_token=? WHERE id=?", (token, account_id)
        )
        report(
            {
                "stage": "Syncing Drafts",
                "detail": provider.info.email,
                "completed": account_index - 1,
                "total": account_total,
            }
        )
        return self.context.sync.sync_account(
            account_id,
            provider,
            since=since,
            force_full=True,
        )

    @Slot()
    def syncAll(self) -> None:
        self._sync_all(force_original_retries=False)

    @Slot()
    def refreshDraftsNow(self) -> None:
        self._sync_all(force_original_retries=False, force_drafts=True)

    @Slot()
    def retryOriginalsNow(self) -> None:
        accounts = list(self.context.repository.list_accounts())
        if not accounts or self._busy:
            return

        def work(report):
            apple_accounts = [item for item in accounts if item["id"].startswith("apple-mail:")]
            discovered = (
                {item.email.casefold(): item for item in discover_apple_mail_accounts()}
                if apple_accounts else {}
            )
            recovered = 0
            outcomes = {"requested": 0, "recovered": 0, "unresolved": 0,
                        "timed_out": 0, "provider_error": 0}
            for index, account in enumerate(accounts, 1):
                report({"stage": f"Retrying originals {index} of {len(accounts)}",
                        "detail": account["email"], "completed": index - 1,
                        "total": len(accounts), "items": recovered})
                if account["id"].startswith("apple-mail:"):
                    info = discovered.get(account["email"].casefold())
                    if info is None:
                        raise RuntimeError("An account is no longer available in Apple Mail")
                    provider = AppleMailProvider(account["id"], info)
                else:
                    provider = self._provider(account["id"])
                recovered += self.context.sync.retry_pending_originals(
                    account["id"], provider, force=True
                )
                for key in outcomes:
                    outcomes[key] += self.context.sync.last_original_retry_outcome.get(key, 0)
            return recovered, outcomes

        def done(result):
            recovered, outcomes = result
            self.reload()
            pending = self.context.repository.pending_original_count()
            detail = (
                f"; Mail timed out on {outcomes['timed_out']}"
                if outcomes["timed_out"] else
                f"; provider retry failed on {outcomes['provider_error']}"
                if outcomes["provider_error"] else
                f"; Mail returned no original for {outcomes['unresolved']}"
                if outcomes["unresolved"] else ""
            )
            self._set_status(f"Original retry finished — {recovered} recovered; "
                             f"{pending} still waiting{detail}")

        self._run("Retrying missing originals…", work, done, reports_progress=True)

    def _sync_all(self, *, force_original_retries: bool, force_drafts: bool = False,
                  automatic: bool = False) -> None:
        if self.syncBusy or self._closing:
            return
        if not self._accounts:
            def loaded(accounts):
                self._accounts = accounts
                self.accountsChanged.emit()
                if accounts:
                    self._sync_all(force_original_retries=force_original_retries,
                                   force_drafts=force_drafts, automatic=automatic)
                else:
                    self._set_status("Add an account in Settings to start importing")
            self._run("Loading accounts…", self.context.repository.list_accounts, loaded)
            return
        self.context.sync.resume()
        self._sync_busy = True
        self._set_busy(True)
        self._set_status("Checking mail — reading and search remain available")
        from email_reader.scheduling import RefreshRound
        self._sync_session = RefreshRound(self.context, self._provider,
            accounts=list(self._accounts), schedule=self._account_schedule,
            invalidate_provider=self._invalidate_provider,
            automatic=automatic, force_drafts=force_drafts or apple_mail_draft_refresh_due(
                getattr(self, "_drafts_last_checked", ""), datetime.now(UTC)))
        self._queue_sync_step()

    def _queue_sync_step(self) -> None:
        session = self._sync_session
        if session is None:
            return
        if self._closing or self.context.sync.paused:
            self._finish_sync_round({"paused": True, "processed": session.processed})
            return
        def done(result):
            if session is not self._sync_session:
                return
            if result.get("updated"):
                self.reload()
                self._start_maintenance()
            if result.get("error"):
                self._set_status("One account needs attention; other accounts will continue")
            if result.get("done"):
                self._finish_sync_round(result)
            else:
                # Yield the provider lane so an explicitly saved draft takes priority.
                QTimer.singleShot(0, self._queue_sync_step)
        self._run("Refreshing mail page…", session.step, done, reports_progress=True)

    def _finish_sync_round(self, result: dict) -> None:
        session = self._sync_session
        if session:
            self.sync_timings["accounts"] = time.monotonic() - session.started
        failed = dict(session.failed) if session else {}
        self._sync_session = None
        self._sync_busy = False
        self._set_busy(bool(self._worker_jobs))
        self.reload()
        now = time.monotonic()
        due = [state["due"] for state in self._account_schedule.values()]
        self._next_automatic_sync_at = min(due) if due else now + 300
        if result.get("paused") or self._closing:
            self._set_status("Import paused — saved progress will resume without starting over")
            self._set_activity(phase="paused", stage="Import paused", detail="Progress saved")
            return
        if failed:
            timeouts = sum(code == "timeout" for code in failed.values())
            self._mail_timeout_failures = self._mail_timeout_failures + 1 if timeouts else 0
            self._set_status(f"Partial refresh — {len(failed)} account(s) need attention; other accounts checked")
            self._set_activity(phase="error", stage="Some accounts need attention",
                               detail="Connection failures retry independently; your saved mail remains available")
            if self._mail_timeout_failures >= 2:
                QTimer.singleShot(0, self.recoverAppleMail)
        else:
            self._mail_timeout_failures = 0
            if session and session.force_drafts:
                self._drafts_last_checked = datetime.now(UTC).isoformat()
                self._run_local(lambda: self.context.save_setting(APPLE_MAIL_DRAFT_REFRESH_KEY,
                                self._drafts_last_checked), lambda _: None)
            checked = datetime.now().astimezone().strftime("%b %d, %H:%M")
            self._set_status(f"Checked {checked} — {result.get('processed', 0)} updates; file indexing continues separately")
            self._set_activity(phase="complete", stage="Mail checked", detail="Reading and search available")
        self.timer.start(max(1000, int((self._next_automatic_sync_at - time.monotonic()) * 1000)))

    @Slot()
    def pauseSync(self) -> None:
        self.context.sync.pause()
        self.timer.stop()
        self.busyChanged.emit()
        self._set_status("Sync will pause after the current message")
        self._set_activity(
            phase="running",
            stage="Pausing safely",
            detail="Finishing the current message before saving progress",
        )

    @Slot()
    def resumeSync(self) -> None:
        self.context.sync.resume()
        self.busyChanged.emit()
        self.syncAll()

    def _invalidate_provider(self, account_id: str) -> None:
        # Called only on the serialized provider worker, never the GUI thread.
        self._providers.pop(account_id, None)
        self.context.database.execute("DELETE FROM app_settings WHERE key=?", (DISCOVERY_CACHE_KEY,))

    def _provider(self, account_id: str):
        if account_id in self._providers:
            return self._providers[account_id]
        row = self.context.database.connection.execute(
            "SELECT provider,email FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row:
            raise KeyError("Account not found")
        if account_id.startswith("apple-mail:"):
            discovered, _ = cached_apple_mail_accounts(self.context.database, {row["email"]})
            info = discovered.get(row["email"].casefold())
            if info is None:
                raise ProviderOperationError("This account is unavailable in Apple Mail")
            provider = AppleMailProvider(account_id, info)
        else:
            provider = (
                GmailProvider(account_id, self.context.vault)
                if row["provider"] == "gmail"
                else OutlookProvider(account_id, self.context.vault)
            )
        provider._email = self.context.database.scalar(
            "SELECT email FROM accounts WHERE id=?", (account_id,), ""
        )
        self._providers[account_id] = provider
        return provider

    @staticmethod
    def _requires_full_apple_import(
        *,
        previous_range: str | None,
        sync_cursor: str | None,
        page_token: str | None,
        requested_range: str | None,
        focused_rebuild: bool,
    ) -> bool:
        return bool(
            previous_range != requested_range or not sync_cursor or page_token or focused_rebuild
        )

    @staticmethod
    def _sync_from(range_name: str, custom_date: str) -> datetime | None:
        now = datetime.now(UTC)
        if range_name == "1 year":
            return now - timedelta(days=365)
        if range_name == "5 years":
            return now - timedelta(days=365 * 5)
        if range_name == "custom" and custom_date:
            return datetime.fromisoformat(custom_date).replace(tzinfo=UTC)
        return None
