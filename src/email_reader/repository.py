from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

from email_reader.categories import infer_mail_category
from email_reader.contracts import (
    Account,
    AttachmentPayload,
    Citation,
    DraftReceipt,
    DraftRequest,
    ExtractedFact,
    MessagePayload,
    SourceRetryRequest,
)
from email_reader.database import Database
from email_reader.document_dates import printed_document_dates
from email_reader.extraction import AttachmentExtractor, ExtractionResult
from email_reader.image_types import visible_mail_file_sql
from email_reader.providers.mime import reply_headers
from email_reader.rendering import preview_snippet
from email_reader.security import EncryptedBlobStore
from email_reader.text import (
    chunk_text,
    clean_sent_body,
    html_to_text,
    looks_like_mislabeled_html,
    message_new_content,
    normalize_message_text,
    safe_fts_query,
)

NAMESPACE = uuid.UUID("e81130c8-76f3-4fd0-8f49-0710d0364de7")


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "\0".join(parts)))


def mailbox_state(labels: list[str], *, is_sent: bool, is_draft: bool) -> tuple[bool, bool, str]:
    label_set = set(labels)
    bulk_labels = {
        "FILTERED_BULK",
        "CATEGORY_PROMOTIONS",
    }
    # Social/forums/list mail is retained. Only explicit promotions are filtered;
    # legacy FILTERED_BULK rows remain repair candidates, never dedup proof.
    is_filtered_bulk = bool(not (is_sent or is_draft) and label_set.intersection(bulk_labels))
    if is_sent or is_draft:
        in_main_inbox = False
    elif "INBOX" in label_set:
        in_main_inbox = not is_filtered_bulk
    else:
        in_main_inbox = not label_set.intersection({"SENT", "DRAFT", "ARCHIVE", *bulk_labels})
    mail_category = (
        "updates"
        if in_main_inbox and "CATEGORY_UPDATES" in label_set
        else ("primary" if in_main_inbox else "")
    )
    return in_main_inbox, is_filtered_bulk, mail_category


class MailRepository:
    def __init__(
        self,
        database: Database,
        blob_store: EncryptedBlobStore,
        extractor: AttachmentExtractor | None = None,
    ) -> None:
        self.db = database
        self.blobs = blob_store
        self.extractor = extractor or AttachmentExtractor()
        self.extraction_runner = None
        self._contact_cache: dict[str, list[dict[str, str]]] = {}

    def upsert_account(self, account: Account) -> None:
        self.db.execute(
            """
            INSERT INTO accounts(id, provider, email, display_name, color, enabled, sync_from, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              email=excluded.email, display_name=excluded.display_name, color=excluded.color,
              enabled=excluded.enabled, sync_from=excluded.sync_from
            """,
            (
                account.id,
                account.provider.value,
                account.email,
                account.display_name,
                account.color,
                int(account.enabled),
                account.sync_from.isoformat() if account.sync_from else None,
                self.db.now(),
            ),
        )

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.query("SELECT * FROM accounts ORDER BY created_at")]

    def repair_malformed_html_bodies(self, message_ids: list[str] | None = None) -> int:
        """Convert HTML mislabeled as plain text and rebuild its searchable chunks."""
        scope = " AND id IN (" + ",".join("?" for _ in message_ids) + ")" if message_ids is not None else ""
        if message_ids == []:
            return 0
        rows = self.db.query(
            """SELECT id,account_id,thread_id,body_text FROM messages
               WHERE body_html=''
                 AND (lower(ltrim(body_text)) LIKE '<!doctype html%'
                   OR lower(ltrim(body_text)) LIKE '<html%'
                   OR lower(ltrim(body_text)) LIKE '<table%'
                   OR lower(body_text) LIKE '%<div%')""" + scope, message_ids or [],
        )
        affected_threads: set[str] = set()
        repaired = 0
        for row in rows:
            if not looks_like_mislabeled_html(row["body_text"]):
                continue
            clean = normalize_message_text(html_to_text(row["body_text"]))
            if not clean or clean == row["body_text"]:
                continue
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE messages SET body_text=?,snippet=?,content_fingerprint='',
                       body_html=CASE WHEN body_html='' THEN ? ELSE body_html END WHERE id=?""",
                    (clean, clean[:240].replace("\n", " "), row["body_text"], row["id"]),
                )
                connection.execute(
                    "DELETE FROM source_chunks WHERE source_type='message' AND source_id=?",
                    (row["id"],),
                )
                for index, content in enumerate(chunk_text(clean)):
                    connection.execute(
                        """INSERT INTO source_chunks(
                           id,source_type,source_id,account_id,thread_id,chunk_index,
                           content,locator,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            stable_id(row["id"], "chunk", str(index)),
                            "message",
                            row["id"],
                            row["account_id"],
                            row["thread_id"],
                            index,
                            content,
                            "",
                            self.db.now(),
                        ),
                    )
                connection.execute(
                    "UPDATE tone_examples SET clean_body=? WHERE message_id=?",
                    (clean_sent_body(clean), row["id"]),
                )
            affected_threads.add(row["thread_id"])
            repaired += 1
        for thread_id in affected_threads:
            self._refresh_thread(thread_id)
        return repaired

    def backfill_mail_categories(self, message_ids: list[str] | None = None) -> int:
        """Classify previously imported focused mail using stable local metadata."""
        if message_ids == []:
            return 0
        scope = " AND id IN (" + ",".join("?" for _ in message_ids) + ")" if message_ids is not None else ""
        rows = self.db.query(
            """SELECT id,sender_email,subject,mail_category FROM messages
               WHERE in_main_inbox=1 AND is_filtered_bulk=0""" + scope, message_ids or [],
        )
        updates = [
            (infer_mail_category(row["sender_email"], row["subject"]), row["id"]) for row in rows
        ]
        if not updates:
            return 0
        with self.db.transaction() as connection:
            connection.executemany("UPDATE messages SET mail_category=? WHERE id=?", updates)
        return len(updates)

    def set_sync_state(
        self,
        account_id: str,
        *,
        cursor: str | None = None,
        page_token: str | None = None,
        completed: bool = False,
    ) -> None:
        self.db.execute(
            """
            UPDATE accounts SET sync_cursor=COALESCE(?,sync_cursor), sync_page_token=?,
              last_synced_at=CASE WHEN ? THEN ? ELSE last_synced_at END
            WHERE id=?
            """,
            (cursor, page_token, int(completed), self.db.now(), account_id),
        )

    def ingest_message(
        self, account_id: str, payload: MessagePayload, *, defer_extraction: bool = False,
    ) -> str:
        if looks_like_mislabeled_html(payload.body_text):
            clean = normalize_message_text(html_to_text(payload.body_text))
            payload = payload.model_copy(
                update={
                    "body_html": payload.body_html or payload.body_text,
                    "body_text": clean,
                    "snippet": clean[:240].replace("\n", " "),
                }
            )
        thread_id = stable_id(account_id, "thread", payload.provider_thread_id)
        message_id = stable_id(account_id, "message", payload.provider_id)
        previous_thread_id = self.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
        if previous_thread_id and self.db.scalar("SELECT trashed_at FROM threads WHERE id=?", (previous_thread_id,)):
            # Provider refresh must never undo the user's local delete, even
            # when a delayed original reveals a different conversation ID.
            return message_id
        if not payload.source_complete:
            self.queue_original_retry(account_id, payload.provider_id, payload.retry_locator)
        if not payload.source_complete and self.db.scalar(
            "SELECT count(*) FROM messages WHERE id=? AND (body_text<>'' OR body_html<>'' OR raw_digest IS NOT NULL)",
            (message_id,),
        ):
            # A temporary provider download failure is not a content revision.
            # Keep the encrypted original and index; flag it for another attempt.
            self.db.execute(
                "UPDATE messages SET is_unread=?,source_complete=0 WHERE id=?",
                (int(payload.is_unread), message_id),
            )
            return message_id
        body_text = payload.body_text or html_to_text(payload.body_html)
        in_main_inbox, is_filtered_bulk, mail_category = mailbox_state(
            payload.labels, is_sent=payload.is_sent, is_draft=payload.is_draft
        )
        raw_digest = None
        if payload.raw_source:
            raw_digest, _ = self.blobs.put(payload.raw_source)
        recipients = {"to": payload.to, "cc": payload.cc, "bcc": payload.bcc,
                      "reply_to": payload.reply_to, "references": payload.references}
        participants = sorted({payload.sender_email, *payload.to, *payload.cc} - {""})

        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO threads(
                  id, account_id, provider_thread_id, subject, latest_at, participants_json,
                  message_count, has_attachments, is_unread, content_hash
                ) VALUES(?,?,?,?,?,?,0,?,?, '')
                ON CONFLICT(account_id, provider_thread_id) DO UPDATE SET
                  subject=excluded.subject,
                  latest_at=MAX(threads.latest_at, excluded.latest_at),
                  participants_json=excluded.participants_json,
                  has_attachments=MAX(threads.has_attachments, excluded.has_attachments),
                  is_unread=MAX(threads.is_unread, excluded.is_unread),
                  source_state='available'
                """,
                (
                    thread_id,
                    account_id,
                    payload.provider_thread_id,
                    payload.subject,
                    payload.received_at.isoformat(),
                    json.dumps(participants),
                    int(bool(payload.attachments)),
                    int(payload.is_unread),
                ),
            )
            connection.execute(
                """
                INSERT INTO messages(
                  id, thread_id, account_id, provider_id, provider_revision, internet_message_id, in_reply_to,
                  subject, sender_name, sender_email, recipients_json, sent_at, received_at,
                  body_text, body_html, snippet, labels_json, is_sent, is_draft, is_unread,
                  in_main_inbox, is_filtered_bulk, mail_category, source_state,
                  raw_digest, web_link, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'available',?,?,?)
                ON CONFLICT(account_id, provider_id) DO UPDATE SET
                  thread_id=excluded.thread_id,
                  provider_revision=COALESCE(excluded.provider_revision,messages.provider_revision),
                  internet_message_id=COALESCE(excluded.internet_message_id,messages.internet_message_id),
                  in_reply_to=COALESCE(excluded.in_reply_to,messages.in_reply_to),
                  subject=excluded.subject, sender_name=excluded.sender_name,
                  sender_email=excluded.sender_email, recipients_json=excluded.recipients_json,
                  sent_at=excluded.sent_at, received_at=excluded.received_at,
                  body_text=excluded.body_text, body_html=excluded.body_html,
                  snippet=excluded.snippet, labels_json=excluded.labels_json,
                  is_sent=excluded.is_sent, is_draft=excluded.is_draft,
                  is_unread=excluded.is_unread, in_main_inbox=excluded.in_main_inbox,
                  is_filtered_bulk=excluded.is_filtered_bulk,
                  mail_category=excluded.mail_category,
                  source_state='available', web_link=excluded.web_link,
                  raw_digest=COALESCE(excluded.raw_digest,messages.raw_digest)
                """,
                (
                    message_id,
                    thread_id,
                    account_id,
                    payload.provider_id,
                    payload.provider_revision,
                    payload.internet_message_id,
                    payload.in_reply_to,
                    payload.subject,
                    payload.sender_name,
                    payload.sender_email,
                    json.dumps(recipients),
                    payload.sent_at.isoformat(),
                    payload.received_at.isoformat(),
                    body_text,
                    payload.body_html,
                    payload.snippet,
                    json.dumps(payload.labels),
                    int(payload.is_sent),
                    int(payload.is_draft),
                    int(payload.is_unread),
                    int(in_main_inbox),
                    int(is_filtered_bulk),
                    mail_category,
                    raw_digest,
                    payload.web_link,
                    self.db.now(),
                ),
            )
            chunks = chunk_text(body_text)
            connection.execute(
                "DELETE FROM source_chunks WHERE source_type='message' AND source_id=? AND chunk_index>=?",
                (message_id, len(chunks)),
            )
            connection.execute(
                "UPDATE messages SET source_complete=?,content_fingerprint=? WHERE id=?",
                (int(payload.source_complete), hashlib.sha256("\0".join((payload.subject, body_text,
                    payload.body_html, payload.sender_email, json.dumps(recipients), payload.sent_at.isoformat())).encode()).hexdigest(), message_id),
            )
            for index, content in enumerate(chunks):
                chunk_id = stable_id(message_id, "chunk", str(index))
                connection.execute(
                    """INSERT INTO source_chunks(
                       id,source_type,source_id,account_id,thread_id,chunk_index,content,locator,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         content=excluded.content,thread_id=excluded.thread_id,account_id=excluded.account_id,
                         embedding_json=CASE WHEN source_chunks.content=excluded.content THEN source_chunks.embedding_json ELSE NULL END,
                         embedding_model=CASE WHEN source_chunks.content=excluded.content THEN source_chunks.embedding_model ELSE NULL END
                       WHERE source_chunks.content<>excluded.content OR source_chunks.thread_id<>excluded.thread_id
                         OR source_chunks.account_id<>excluded.account_id""",
                    (
                        chunk_id,
                        "message",
                        message_id,
                        account_id,
                        thread_id,
                        index,
                        content,
                        "",
                        self.db.now(),
                    ),
                )

        for attachment in payload.attachments:
            self._ingest_attachment(
                account_id, thread_id, message_id, attachment, defer_extraction=defer_extraction,
            )
        self._update_contacts(account_id, message_id, payload.sender_name, payload.sender_email,
                              recipients, payload.is_sent, payload.sent_at.isoformat())
        self._refresh_thread(thread_id)
        if previous_thread_id and previous_thread_id != thread_id:
            self.db.execute("UPDATE source_chunks SET thread_id=? WHERE source_type='attachment' AND source_id IN(SELECT id FROM attachments WHERE message_id=?)", (thread_id, message_id))
            self._refresh_thread(previous_thread_id)
            if not self.db.scalar("SELECT count(*) FROM messages WHERE thread_id=?", (previous_thread_id,)):
                self.db.execute("UPDATE draft_records SET thread_id=? WHERE thread_id=?", (thread_id, previous_thread_id))
                self.db.execute("UPDATE threads SET important_override=COALESCE(important_override,(SELECT important_override FROM threads WHERE id=?)) WHERE id=?", (previous_thread_id, thread_id))
        if payload.is_sent and not payload.is_draft:
            self._upsert_tone_example(account_id, message_id, recipients, body_text)
            self._match_sent_draft(account_id, message_id)
        if payload.source_complete:
            self.db.execute("DELETE FROM source_retry_queue WHERE account_id=? AND provider_id=?", (account_id, payload.provider_id))
        if payload.is_draft and payload.raw_source:
            self.reconcile_saved_draft_mirrors(account_id)
        if defer_extraction and body_text and not payload.is_draft:
            self.db.execute(
                """INSERT INTO ai_enrichment_jobs(message_id,body_hash,next_attempt_at) VALUES(?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET body_hash=excluded.body_hash,attempts=0,
                     next_attempt_at=excluded.next_attempt_at
                   WHERE ai_enrichment_jobs.body_hash<>excluded.body_hash""",
                (message_id, hashlib.sha256(body_text.encode()).hexdigest(), self.db.now()),
            )
        return message_id

    def get_import_checkpoint(self, account_id: str, job_kind: str, policy_version: str) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM import_checkpoints WHERE account_id=? AND job_kind=? AND policy_version=?",
            (account_id, job_kind, policy_version),
        )
        return dict(rows[0]) if rows else None

    def save_import_checkpoint(
        self, account_id: str, job_kind: str, policy_version: str, *,
        cursor: str | None, page_token: str | None, complete: bool = False,
        since: datetime | str | None = None,
    ) -> None:
        since = since.isoformat() if isinstance(since, datetime) else since
        self.db.execute(
            """INSERT INTO import_checkpoints
               (account_id,job_kind,policy_version,cursor,page_token,status,since,started_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,job_kind,policy_version)
               DO UPDATE SET cursor=excluded.cursor,page_token=excluded.page_token,
                 status=excluded.status,since=COALESCE(excluded.since,import_checkpoints.since),
                 updated_at=excluded.updated_at""",
            (account_id, job_kind, policy_version, cursor, page_token,
             "complete" if complete else "running", since, self.db.now(), self.db.now()),
        )

    def source_retry_not_due_ids(self, account_id: str) -> set[str]:
        return {row["provider_id"] for row in self.db.query(
            "SELECT provider_id FROM source_retry_queue WHERE account_id=? AND next_attempt_at>?",
            (account_id, self.db.now()),
        )}

    def record_import_exclusions(self, account_id: str, decisions: dict[str, str], policy_version: str) -> None:
        if not decisions:
            return
        stamp = self.db.now()
        with self.db.transaction() as connection:
            connection.executemany(
                """INSERT INTO import_exclusions(account_id,provider_id,policy_version,reason,recorded_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(account_id,provider_id) DO UPDATE SET
                     policy_version=excluded.policy_version,reason=excluded.reason,recorded_at=excluded.recorded_at""",
                [(account_id, identifier, policy_version, reason, stamp)
                 for identifier, reason in decisions.items() if reason in {"promotions", "junk"}],
            )

    def known_import_exclusions(self, account_id: str, policy_version: str) -> set[str]:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        return {row["provider_id"] for row in self.db.query(
            "SELECT provider_id FROM import_exclusions WHERE account_id=? AND policy_version=? AND recorded_at>?",
            (account_id, policy_version, cutoff),
        )}

    def restore_main_inbox_ids(self, account_id: str, provider_ids: list[str] | set[str]) -> None:
        """Positive provider membership repairs local visibility, never user/provider flags."""
        if not provider_ids:
            return
        with self.db.transaction() as connection:
            connection.executemany(
                """UPDATE messages SET in_main_inbox=1,
                     mail_category=CASE WHEN mail_category='' THEN 'primary' ELSE mail_category END
                   WHERE account_id=? AND provider_id=? AND source_state='available'
                     AND is_filtered_bulk=0 AND is_sent=0 AND is_draft=0
                     AND EXISTS(SELECT 1 FROM threads t WHERE t.id=messages.thread_id AND t.trashed_at IS NULL)""",
                [(account_id, identifier) for identifier in provider_ids],
            )

    def queue_original_retry(self, account_id: str, provider_id: str, locator: dict[str, str]) -> None:
        self.db.execute(
            """INSERT INTO source_retry_queue(account_id,provider_id,locator_json,next_attempt_at)
               VALUES(?,?,?,?) ON CONFLICT(account_id,provider_id) DO UPDATE SET
               locator_json=CASE WHEN excluded.locator_json<>'{}' THEN excluded.locator_json ELSE source_retry_queue.locator_json END""",
            (account_id, provider_id, json.dumps(locator), self.db.now()),
        )

    def pending_originals(self, account_id: str, limit: int = 8, *, force: bool = False) -> list[SourceRetryRequest]:
        # Backfill old deferred imports too: a recent-date overlap is not a
        # durable retry mechanism. No completed original enters this queue.
        for row in self.db.query(
            """SELECT m.provider_id,m.is_draft,m.is_sent FROM messages m JOIN threads t ON t.id=m.thread_id
               WHERE m.account_id=? AND m.source_complete=0 AND m.source_state='available' AND t.trashed_at IS NULL
                 AND NOT EXISTS(SELECT 1 FROM source_retry_queue q WHERE q.account_id=m.account_id AND q.provider_id=m.provider_id)""",
            (account_id,),
        ):
            self.queue_original_retry(account_id, row["provider_id"], {
                "role": "drafts" if row["is_draft"] else ("sent" if row["is_sent"] else "inbox"),
            })
        return [SourceRetryRequest(provider_id=row["provider_id"], locator=json.loads(row["locator_json"]))
            for row in self.db.query(
                """SELECT q.* FROM source_retry_queue q JOIN messages m
                   ON m.account_id=q.account_id AND m.provider_id=q.provider_id JOIN threads t ON t.id=m.thread_id
                   WHERE q.account_id=? AND (? OR q.next_attempt_at<=?) AND t.trashed_at IS NULL
                   ORDER BY q.next_attempt_at,q.provider_id LIMIT ?""", (account_id, int(force), self.db.now(), limit))]

    def original_retry_attempted(self, account_id: str, provider_id: str) -> None:
        attempts = self.db.scalar("SELECT attempts FROM source_retry_queue WHERE account_id=? AND provider_id=?", (account_id, provider_id), 0)
        # Mail may spend tens of seconds failing the same unavailable source.
        # Keep retries durable, but do not re-block every five-minute refresh.
        delay = min(86_400, 1_800 * (2 ** min(int(attempts), 6)))
        self.db.execute("UPDATE source_retry_queue SET attempts=attempts+1,next_attempt_at=? WHERE account_id=? AND provider_id=?",
            ((datetime.now(UTC) + timedelta(seconds=delay)).isoformat(), account_id, provider_id))

    def resolve_excluded_original(self, account_id: str, provider_id: str) -> None:
        # A metadata-only placeholder can later turn out to be filtered bulk.
        # Retain it locally but remove it from the focused inbox and retry queue.
        self.db.execute("UPDATE messages SET source_complete=1,is_filtered_bulk=1,in_main_inbox=0 WHERE account_id=? AND provider_id=?",
            (account_id, provider_id))
        self.db.execute("DELETE FROM source_retry_queue WHERE account_id=? AND provider_id=?", (account_id, provider_id))

    def pending_original_count(self) -> int:
        return self.db.scalar("SELECT COUNT(*) FROM source_retry_queue q JOIN messages m ON m.account_id=q.account_id AND m.provider_id=q.provider_id JOIN threads t ON t.id=m.thread_id WHERE t.trashed_at IS NULL", default=0)

    def complete_message_ids(
        self, account_id: str, *, include_drafts: bool = False
    ) -> set[str]:
        # Apple Mail does not expose a Draft revision. Gmail's *underlying
        # message* ID changes on every Draft edit, so its provider may opt in.
        return {
            row["provider_id"]
            for row in self.db.query(
                "SELECT provider_id FROM messages WHERE account_id=? AND source_complete=1 "
                "AND (?=1 OR is_draft=0) AND source_state='available' AND is_filtered_bulk=0",
                (account_id, int(include_drafts)),
            )
        }

    def complete_message_revisions(self, account_id: str) -> dict[str, str]:
        return {
            row["provider_id"]: row["provider_revision"]
            for row in self.db.query(
                "SELECT provider_id,provider_revision FROM messages "
                "WHERE account_id=? AND source_complete=1 AND source_state='available' "
                "AND provider_revision IS NOT NULL",
                (account_id,),
            )
        }

    def available_draft_ids(self, account_id: str) -> set[str]:
        return {
            row["provider_id"]
            for row in self.db.query(
                "SELECT provider_id FROM messages WHERE account_id=? AND is_draft=1 "
                "AND source_state='available'",
                (account_id,),
            )
        }

    def update_provider_revisions(self, account_id: str, revisions: dict[str, str]) -> None:
        if revisions:
            with self.db.transaction() as connection:
                connection.executemany(
                    "UPDATE messages SET provider_revision=? "
                    "WHERE account_id=? AND provider_id=? AND source_complete=1",
                    [(revision, account_id, identifier)
                     for identifier, revision in revisions.items()],
                )

    def update_provider_read_states(self, account_id: str, states: dict[str, bool]) -> None:
        if states:
            with self.db.transaction() as connection:
                connection.executemany(
                    "UPDATE messages SET is_unread=? WHERE account_id=? AND provider_id=?",
                    [
                        (int(unread), account_id, identifier)
                        for identifier, unread in states.items()
                    ],
                )

    def update_provider_labels(
        self, account_id: str, states: dict[str, list[str]],
        *, preserve_read_state: bool = False,
    ) -> None:
        """Apply folder/label changes without replacing encrypted content."""
        if not states:
            return
        with self.db.transaction() as connection:
            for identifier, labels in states.items():
                is_sent = "SENT" in labels
                is_draft = "DRAFT" in labels
                in_main_inbox, is_filtered_bulk, mail_category = mailbox_state(
                    labels, is_sent=is_sent, is_draft=is_draft
                )
                read_sql = "" if preserve_read_state else "is_unread=?,"
                values = [json.dumps(labels), int(is_sent), int(is_draft)]
                if not preserve_read_state:
                    values.append(int("UNREAD" in labels))
                values.extend((int(in_main_inbox), int(is_filtered_bulk),
                               mail_category, account_id, identifier))
                connection.execute(
                    "UPDATE messages SET labels_json=?,is_sent=?,is_draft=?,"
                    + read_sql + "in_main_inbox=?,is_filtered_bulk=?,mail_category=? "
                    "WHERE account_id=? AND provider_id=? AND source_complete=1 "
                    "AND source_state='available'",
                    values,
                )

    def remove_provider_folder_roles(self, account_id: str, roles: dict[str, str]) -> None:
        """Handle Graph folder moves without declaring a live source deleted."""
        label_for_role = {"inbox": "INBOX", "sentitems": "SENT", "drafts": "DRAFT"}
        if any(role not in label_for_role for role in roles.values()):
            raise ValueError("Unknown provider folder role")
        with self.db.transaction() as connection:
            for identifier, role in roles.items():
                row = connection.execute(
                    "SELECT id,labels_json FROM messages WHERE account_id=? "
                    "AND provider_id=? AND source_state='available'",
                    (account_id, identifier),
                ).fetchone()
                if row is None:
                    continue
                labels = [label for label in json.loads(row["labels_json"])
                          if label != label_for_role[role]]
                if not set(labels).intersection({"INBOX", "SENT", "DRAFT", "ARCHIVE"}):
                    labels.append("ARCHIVE")
                is_sent = "SENT" in labels
                is_draft = "DRAFT" in labels
                in_main_inbox, is_filtered_bulk, mail_category = mailbox_state(
                    labels, is_sent=is_sent, is_draft=is_draft
                )
                connection.execute(
                    "UPDATE messages SET labels_json=?,is_sent=?,is_draft=?,"
                    "in_main_inbox=?,is_filtered_bulk=?,mail_category=? WHERE id=?",
                    (json.dumps(labels), int(is_sent), int(is_draft),
                     int(in_main_inbox), int(is_filtered_bulk), mail_category, row["id"]),
                )

    def record_saved_draft(
        self,
        account_id: str,
        receipt: DraftReceipt,
        request: DraftRequest,
    ) -> str:
        """Mirror a provider-confirmed draft locally so Drafts updates immediately."""
        account = self.db.connection.execute(
            "SELECT email,display_name FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not account:
            raise KeyError(f"Unknown account {account_id}")
        provider_id = receipt.provider_message_id or receipt.provider_draft_id
        now = datetime.now(UTC)
        return self.ingest_message(
            account_id,
            MessagePayload(
                provider_id=provider_id,
                provider_thread_id=request.provider_thread_id or f"draft:{provider_id}",
                subject=request.subject or "(no subject)",
                sender_name=account["display_name"] or account["email"],
                sender_email=account["email"],
                to=request.to,
                cc=request.cc,
                bcc=request.bcc,
                sent_at=now,
                received_at=now,
                body_text=request.body_text,
                snippet=request.body_text[:240].replace("\n", " "),
                labels=["DRAFT"],
                is_draft=True,
                web_link=receipt.web_link,
                attachments=request.attachments,
            ),
        )

    def reconcile_saved_draft_mirrors(self, account_id: str | None = None) -> int:
        """Remove only proven local mirrors after the same saved draft is imported.

        Apple Mail's outgoing-message ID can differ from the mailbox message ID.
        The immediate local mirror is therefore temporary, not a second draft.
        Ambiguous or edited drafts are left untouched rather than conflated.
        """
        mirrors = self.db.query(
            """SELECT DISTINCT m.id,m.thread_id,m.account_id,m.provider_id,m.subject,
                      m.body_text,m.recipients_json,m.sent_at
               FROM draft_records d JOIN messages m
                 ON m.account_id=d.account_id AND m.provider_id=d.provider_draft_id
               WHERE m.is_draft=1 AND m.raw_digest IS NULL
                 AND (? IS NULL OR m.account_id=?)""",
            (account_id, account_id),
        )
        removed = 0
        for mirror in mirrors:
            if not mirror["body_text"].strip():
                continue
            imported = self.db.query(
                """SELECT id,thread_id,body_text,recipients_json FROM messages
                   WHERE account_id=? AND is_draft=1 AND id<>? AND subject=?
                     AND raw_digest IS NOT NULL
                     AND ABS(julianday(sent_at)-julianday(?)) <= 1""",
                (mirror["account_id"], mirror["id"], mirror["subject"], mirror["sent_at"]),
            )
            expected = json.loads(mirror["recipients_json"])
            expected_to = [str(item).casefold() for item in expected.get("to", [])]
            expected_cc = [str(item).casefold() for item in expected.get("cc", [])]
            matches = []
            for candidate in imported:
                recipients = json.loads(candidate["recipients_json"])
                if (
                    normalize_message_text(candidate["body_text"])
                    == normalize_message_text(mirror["body_text"])
                    and [str(item).casefold() for item in recipients.get("to", [])] == expected_to
                    and [str(item).casefold() for item in recipients.get("cc", [])] == expected_cc
                ):
                    matches.append(candidate)
            if len(matches) != 1:
                continue
            target_thread = matches[0]["thread_id"]
            with self.db.transaction() as connection:
                connection.execute("DELETE FROM extracted_facts WHERE message_id=?", (mirror["id"],))
                connection.execute("DELETE FROM tone_examples WHERE message_id=?", (mirror["id"],))
                connection.execute(
                    "DELETE FROM source_chunks WHERE source_type='attachment' AND source_id IN "
                    "(SELECT id FROM attachments WHERE message_id=?)",
                    (mirror["id"],),
                )
                connection.execute("DELETE FROM attachments WHERE message_id=?", (mirror["id"],))
                connection.execute(
                    "DELETE FROM source_chunks WHERE source_type='message' AND source_id=?",
                    (mirror["id"],),
                )
                connection.execute(
                    "DELETE FROM source_retry_queue WHERE account_id=? AND provider_id=?",
                    (mirror["account_id"], mirror["provider_id"]),
                )
                connection.execute("DELETE FROM messages WHERE id=?", (mirror["id"],))
                if not connection.execute(
                    "SELECT 1 FROM messages WHERE thread_id=? LIMIT 1", (mirror["thread_id"],)
                ).fetchone():
                    connection.execute(
                        "UPDATE draft_records SET thread_id=? WHERE thread_id=?",
                        (target_thread, mirror["thread_id"]),
                    )
                    connection.execute("DELETE FROM summaries WHERE thread_id=?", (mirror["thread_id"],))
                    connection.execute("DELETE FROM source_chunks WHERE thread_id=?", (mirror["thread_id"],))
                    connection.execute("DELETE FROM threads WHERE id=?", (mirror["thread_id"],))
            self._refresh_thread(target_thread)
            if mirror["thread_id"] != target_thread and self.db.scalar(
                "SELECT 1 FROM threads WHERE id=?", (mirror["thread_id"],)
            ):
                self._refresh_thread(mirror["thread_id"])
            removed += 1
        return removed

    def _ingest_attachment(
        self, account_id: str, thread_id: str, message_id: str, attachment: Any, *,
        defer_extraction: bool = False, expected_digest: str | None = None, extraction_result=None,
    ) -> bool:
        attachment_id = stable_id(message_id, "attachment", attachment.provider_id)
        digest = None
        extraction_text = ""
        metadata: dict[str, Any] = {}
        byte_size = attachment.size
        reuse_extraction = False
        shared_extraction = False
        if attachment.content is not None:
            digest, byte_size = self.blobs.put(attachment.content)
            previous = self.db.connection.execute(
                "SELECT content_digest,filename,mime_type,extracted_text,extraction_metadata_json "
                "FROM attachments WHERE id=?", (attachment_id,),
            ).fetchone()
            previous_metadata = (
                json.loads(previous["extraction_metadata_json"]) if previous else {}
            )
            # A successful OCR result can be empty (e.g. a signature icon).
            # Keep a versioned completion marker, never confuse failed or
            # unsupported extraction with successfully finding no text.
            reuse_extraction = bool(
                previous and previous["content_digest"] == digest
                and previous["filename"] == attachment.filename
                and previous["mime_type"] == attachment.mime_type
                and previous_metadata.get("_extractor_version") == self.extractor.version_for(
                    attachment.filename, attachment.mime_type
                )
                and previous_metadata.get("_extraction_complete") is True
                and not previous_metadata.get("warnings")
                and (len(chunk_text(previous["extracted_text"])) == self.db.scalar(
                    "SELECT count(*) FROM source_chunks WHERE source_type='attachment' "
                    "AND source_id=? AND chunk_index>=0", (attachment_id,), 0,
                ))
            )
            if reuse_extraction:
                extraction_text = previous["extracted_text"]
                metadata = previous_metadata
            else:
                cached = self._cached_attachment_extraction(digest, attachment.filename, attachment.mime_type,
                                                            exclude_id=attachment_id)
                shared_extraction = cached is not None
                if defer_extraction and cached is None:
                    metadata = {"_extraction_complete": False, "_indexing_state": "pending"}
                else:
                    result = cached if cached is not None else extraction_result
                    if result is None:
                        result = self.extractor.extract(attachment.filename, attachment.mime_type, attachment.content)
                    extraction_text = result.text
                    metadata = {
                        **result.metadata, "warnings": result.warnings,
                        "_extractor_version": self.extractor.version_for(attachment.filename, attachment.mime_type),
                        "_extraction_complete": not result.warnings,
                    }
        with self.db.transaction() as connection:
            if expected_digest is not None:
                current = connection.execute(
                    "SELECT content_digest FROM attachments WHERE id=?", (attachment_id,),
                ).fetchone()
                if not current or current["content_digest"] != expected_digest:
                    return False
            connection.execute(
                """
                INSERT INTO attachments(
                  id,message_id,provider_id,filename,mime_type,byte_size,content_digest,
                  extracted_text,extraction_metadata_json,is_inline,created_at,content_id,
                  content_location
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_id,provider_id) DO UPDATE SET
                  filename=excluded.filename,mime_type=excluded.mime_type,byte_size=excluded.byte_size,
                  content_digest=COALESCE(excluded.content_digest,attachments.content_digest),
                  extracted_text=CASE WHEN excluded.content_digest IS NOT NULL THEN excluded.extracted_text ELSE attachments.extracted_text END,
                  extraction_metadata_json=CASE WHEN excluded.content_digest IS NOT NULL THEN excluded.extraction_metadata_json ELSE attachments.extraction_metadata_json END,
                  is_inline=excluded.is_inline,content_id=excluded.content_id,
                  content_location=excluded.content_location
                """,
                (
                    attachment_id,
                    message_id,
                    attachment.provider_id,
                    attachment.filename,
                    attachment.mime_type,
                    byte_size,
                    digest,
                    extraction_text,
                    json.dumps(metadata),
                    int(attachment.is_inline),
                    self.db.now(),
                    attachment.content_id,
                    attachment.content_location,
                ),
            )
            if digest:
                connection.execute(
                    "UPDATE attachments SET content_fingerprint=? WHERE id=?",
                    (hashlib.sha256("\0".join((digest, extraction_text,
                         json.dumps(metadata, sort_keys=True))).encode()).hexdigest(), attachment_id),
                )
            if attachment.content is not None and not reuse_extraction:
                connection.execute(
                    "DELETE FROM source_chunks WHERE source_type='attachment' AND source_id=?",
                    (attachment_id,),
                )
                chunks = chunk_text(extraction_text)
                for index, (content, locator) in enumerate(zip(
                    chunks, self._attachment_chunk_locators(chunks), strict=True
                )):
                    connection.execute(
                        """INSERT INTO source_chunks(
                           id,source_type,source_id,account_id,thread_id,chunk_index,content,locator,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            stable_id(attachment_id, "chunk", str(index)),
                            "attachment",
                            attachment_id,
                            account_id,
                            thread_id,
                            index,
                            content,
                            locator,
                            self.db.now(),
                        ),
                    )
            if defer_extraction and digest and not reuse_extraction and not shared_extraction:
                connection.execute(
                    """INSERT INTO enrichment_jobs(attachment_id,content_digest,next_attempt_at,created_at)
                       VALUES(?,?,?,?) ON CONFLICT(attachment_id) DO UPDATE SET
                         attempts=CASE WHEN enrichment_jobs.content_digest=excluded.content_digest
                           THEN enrichment_jobs.attempts ELSE 0 END,
                         next_attempt_at=CASE WHEN enrichment_jobs.content_digest=excluded.content_digest
                           THEN enrichment_jobs.next_attempt_at ELSE excluded.next_attempt_at END,
                         content_digest=excluded.content_digest""",
                    (attachment_id, digest, self.db.now(), self.db.now()),
                )
        self.index_attachment_metadata(attachment_id)
        return True

    def _cached_attachment_extraction(self, digest: str, filename: str, mime_type: str,
                                     *, exclude_id: str = "") -> ExtractionResult | None:
        """Reuse identical completed originals, keeping each source's own citation chunks."""
        rows = self.db.query(
            """SELECT filename,extracted_text,extraction_metadata_json FROM attachments
               WHERE content_digest=? AND mime_type=? AND id<>?
                 AND json_extract(extraction_metadata_json,'$._extraction_complete')=1
                 AND json_extract(extraction_metadata_json,'$._extractor_version')=? LIMIT 16""",
            (digest, mime_type, exclude_id, self.extractor.version_for(filename, mime_type)),
        )
        for row in rows:
            if Path(row["filename"]).suffix.casefold() != Path(filename).suffix.casefold():
                continue
            metadata = json.loads(row["extraction_metadata_json"])
            if not metadata.get("warnings"):
                return ExtractionResult(row["extracted_text"], metadata, [])
        return None

    def process_enrichment(self, limit: int = 8, should_cancel=None) -> dict[str, int]:
        """Bounded durable OCR/text work; originals remain usable while this runs."""
        if not 1 <= limit <= 100:
            raise ValueError("Enrichment batch size must be between 1 and 100")
        rows = self.db.query(
            """SELECT a.*,m.account_id,m.thread_id,j.attempts,j.content_digest AS queued_digest
               FROM enrichment_jobs j JOIN attachments a ON a.id=j.attachment_id
               JOIN messages m ON m.id=a.message_id JOIN threads t ON t.id=m.thread_id
               WHERE j.next_attempt_at<=? AND t.trashed_at IS NULL
               ORDER BY j.created_at LIMIT ?""", (self.db.now(), limit),
        )
        completed = failed = 0
        for row in rows:
            if should_cancel and should_cancel():
                break
            try:
                original = self.blobs.get(row["queued_digest"])
                extracted = self._cached_attachment_extraction(row["queued_digest"], row["filename"], row["mime_type"], exclude_id=row["id"])
                if extracted is None and self.extraction_runner:
                    extracted = self.extraction_runner(row["filename"], row["mime_type"], original, should_cancel=should_cancel)
                if should_cancel and should_cancel():
                    break
                applied = self._ingest_attachment(
                    row["account_id"], row["thread_id"], row["message_id"],
                    AttachmentPayload(
                        provider_id=row["provider_id"], filename=row["filename"],
                        mime_type=row["mime_type"], size=row["byte_size"], content=original,
                        is_inline=bool(row["is_inline"]), content_id=row["content_id"] or "",
                        content_location=row["content_location"] or "",
                    ), expected_digest=row["queued_digest"], extraction_result=extracted,
                )
                if applied:
                    self._refresh_thread(row["thread_id"])
                self.db.execute(
                    "DELETE FROM enrichment_jobs WHERE attachment_id=? AND content_digest=?",
                    (row["id"], row["queued_digest"]),
                )
                completed += int(applied)
            except Exception:
                if should_cancel and should_cancel():
                    break
                # Persist only a retry count/time, never private exception text.
                delay = min(3600, 60 * 2 ** min(int(row["attempts"]), 6))
                self.db.execute(
                    "UPDATE enrichment_jobs SET attempts=attempts+1,next_attempt_at=? "
                    "WHERE attachment_id=? AND content_digest=?",
                    ((datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                     row["id"], row["queued_digest"]),
                )
                failed += 1
        return {"completed": completed, "failed": failed, "remaining": self.pending_enrichment_count()}

    def pending_enrichment_count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM enrichment_jobs", default=0))

    def reindex_outdated_pdf_extractions(self, limit: int = 20) -> dict[str, int]:
        """Rebuild old PDF text from encrypted local originals, without provider reads."""
        return self._reindex_outdated_extractions(
            "lower(a.filename) LIKE '%.pdf' OR lower(a.mime_type)='application/pdf'",
            self.extractor.CACHE_VERSION, limit,
        )

    def reindex_outdated_text_extractions(self, limit: int = 20) -> dict[str, int]:
        """Recover UTF-16 text/CSV/TSV from encrypted originals, without provider reads."""
        suffixes = tuple(sorted(self.extractor.TEXT_SUFFIXES))
        media_types = tuple(sorted(self.extractor.TEXT_TYPES))
        predicate = " OR ".join(
            [*(f"lower(a.filename) LIKE '%{suffix}'" for suffix in suffixes),
             *(f"(lower(a.mime_type)='{media_type}' OR "
               f"lower(a.mime_type) LIKE '{media_type};%')" for media_type in media_types)]
        )
        return self._reindex_outdated_extractions(
            predicate, self.extractor.TEXT_CACHE_VERSION, limit,
        )

    def reindex_outdated_office_extractions(self, limit: int = 20) -> dict[str, int]:
        """Recover previously omitted Office text from local originals."""
        return self._reindex_outdated_extractions(
            "lower(a.filename) LIKE '%.docx' OR lower(a.filename) LIKE '%.xlsx' "
            "OR lower(a.filename) LIKE '%.xlsm' OR lower(a.filename) LIKE '%.pptx'",
            self.extractor.OFFICE_CACHE_VERSION, limit,
        )

    def _reindex_outdated_extractions(
        self, type_predicate: str, version: int, limit: int,
    ) -> dict[str, int]:
        if not 1 <= limit <= 100:
            raise ValueError("Local reindex batch size must be between 1 and 100")
        predicate = f"""a.content_digest IS NOT NULL AND ({type_predicate})
            AND coalesce(cast(json_extract(a.extraction_metadata_json,
                '$._extractor_version') AS INTEGER),0) < ?"""
        rows = self.db.query(
            f"""SELECT a.provider_id,a.filename,a.mime_type,a.byte_size,a.content_digest,
                       a.is_inline,a.content_id,a.content_location,a.message_id,m.account_id,m.thread_id
                FROM attachments a JOIN messages m ON m.id=a.message_id
                WHERE {predicate} ORDER BY a.created_at,a.id LIMIT ?""",
            (version, limit),
        )
        for row in rows:
            original = self.blobs.get(row["content_digest"])
            self._ingest_attachment(
                row["account_id"], row["thread_id"], row["message_id"],
                AttachmentPayload(
                    provider_id=row["provider_id"], filename=row["filename"],
                    mime_type=row["mime_type"], size=row["byte_size"],
                    content=original, is_inline=bool(row["is_inline"]),
                    content_id=row["content_id"] or "",
                    content_location=row["content_location"] or "",
                ),
            )
            # The same original can now expose previously unseen OCR facts.
            # A summary made before reindexing must not mask those facts.
            self.db.execute("DELETE FROM summaries WHERE thread_id=?", (row["thread_id"],))
        remaining = self.db.scalar(
            f"SELECT count(*) FROM attachments a WHERE {predicate}",
            (version,), 0,
        )
        return {"updated": len(rows), "remaining": int(remaining)}

    def index_attachment_metadata(self, attachment_id: str) -> None:
        row = self.db.connection.execute(
            """SELECT a.id,a.filename,m.account_id,m.thread_id,m.subject,m.sender_email,m.sent_at,
                      substr(m.body_text,1,900) AS covering_text
               FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.id=?""",
            (attachment_id,),
        ).fetchone()
        if not row:
            return
        content = (
            f"Attached file: {row['filename']}\nCovering email subject: {row['subject']}\n"
            f"Email date: {row['sent_at']}\nSender: {row['sender_email']}\n"
            f"Covering email excerpt (not extracted file content):\n{row['covering_text']}"
        )
        self.db.execute(
            """INSERT INTO source_chunks(id,source_type,source_id,account_id,thread_id,chunk_index,content,locator,created_at)
               VALUES(?,'attachment',?,?,?,-1,?,'Attachment details and covering email',?)
               ON CONFLICT(id) DO UPDATE SET content=excluded.content,thread_id=excluded.thread_id,
                 embedding_json=CASE WHEN source_chunks.content=excluded.content THEN source_chunks.embedding_json ELSE NULL END,
                 embedding_model=CASE WHEN source_chunks.content=excluded.content THEN source_chunks.embedding_model ELSE NULL END
               WHERE source_chunks.content<>excluded.content OR source_chunks.thread_id<>excluded.thread_id""",
            (
                stable_id(attachment_id, "metadata"),
                attachment_id,
                row["account_id"],
                row["thread_id"],
                content,
                self.db.now(),
            ),
        )

    def backfill_attachment_metadata(self, limit: int | None = None) -> int:
        rows = self.db.query(
            """SELECT a.id FROM attachments a WHERE NOT EXISTS(
                SELECT 1 FROM source_chunks c WHERE c.source_type='attachment' AND c.source_id=a.id AND c.chunk_index=-1)
                LIMIT ?""", (limit if limit is not None else -1,),
        )
        for row in rows:
            self.index_attachment_metadata(row["id"])
        return len(rows)

    @staticmethod
    def _locator_for_chunk(content: str) -> str:
        return ", ".join(dict.fromkeys(re.findall(
            r"\[(Page \d+|Sheet: [^\]]+|Slide \d+)\]", content
        )))

    @staticmethod
    def _attachment_chunk_locators(chunks: list[str]) -> list[str]:
        """Label all pages/sheets/slides, including headerless continuations."""
        previous = ""
        locators = []
        for content in chunks:
            matches = list(re.finditer(r"\[(Page \d+|Sheet: [^\]]+|Slide \d+)\]", content))
            labels = [previous] if previous and (not matches or content[:matches[0].start()].strip()) else []
            labels.extend(match.group(1) for match in matches)
            locators.append(", ".join(dict.fromkeys(labels)))
            if matches:
                previous = matches[-1].group(1)
        return locators

    def backfill_attachment_locators(self, attachment_ids: list[str] | None = None) -> int:
        """Repair labels only; preserve chunk IDs, text, embeddings and originals."""
        if attachment_ids == []:
            return 0
        scope = " AND source_id IN (" + ",".join("?" for _ in attachment_ids) + ")" if attachment_ids is not None else ""
        rows = self.db.query(
            "SELECT id,source_id,content,locator FROM source_chunks "
            "WHERE source_type='attachment' AND chunk_index>=0" + scope + " ORDER BY source_id,chunk_index",
            attachment_ids or [],
        )
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["source_id"], []).append(row)
        updates = []
        for chunks in grouped.values():
            for row, locator in zip(chunks, self._attachment_chunk_locators(
                [chunk["content"] for chunk in chunks]
            ), strict=True):
                if locator != (row["locator"] or ""):
                    updates.append((locator, row["id"]))
        if updates:
            with self.db.transaction() as connection:
                connection.executemany("UPDATE source_chunks SET locator=? WHERE id=?", updates)
        return len(updates)

    def maintain_legacy_indexes(self, limit: int = 100) -> bool:
        """One durable bounded migration slice; True means another slice is needed."""
        for key, version, table, work in (
            ("attachment_locator_version", "v1", "attachments", self.backfill_attachment_locators),
            ("html_body_repair_version", "v2", "messages", self.repair_malformed_html_bodies),
            ("mail_category_version", "local-categories-v1", "messages", self.backfill_mail_categories),
        ):
            if self.db.scalar("SELECT value FROM app_settings WHERE key=?", (key,), "") == version:
                continue
            cursor_key = key + ":cursor:" + version
            offset = int(self.db.scalar("SELECT value FROM app_settings WHERE key=?", (cursor_key,), "0"))
            rows = self.db.query(f"SELECT rowid,id FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?", (offset, limit))
            work([row["id"] for row in rows])
            with self.db.transaction() as connection:
                if rows:
                    connection.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                       (cursor_key, str(rows[-1]["rowid"])))
                if len(rows) < limit:
                    connection.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, version))
            return True
        return False

    def _refresh_thread(self, thread_id: str) -> None:
        with self.db.transaction():
            self._refresh_thread_revision(thread_id)

    def _refresh_thread_revision(self, thread_id: str) -> None:
        rows = self.db.query(
            """SELECT id,content_fingerprint,subject,
                      CASE WHEN content_fingerprint='' THEN body_text ELSE '' END AS body_text
               FROM messages WHERE thread_id=? ORDER BY sent_at,id""",
            (thread_id,),
        )
        hasher = hashlib.sha256()
        missing_message_fingerprints = []
        for row in rows:
            fingerprint = row["content_fingerprint"] or hashlib.sha256(
                f"{row['subject']}\0{row['body_text']}".encode()).hexdigest()
            hasher.update(f"{row['id']}:{fingerprint}".encode())
            if not row["content_fingerprint"]:
                missing_message_fingerprints.append((fingerprint, row["id"]))
        missing_attachment_fingerprints = []
        for attachment in self.db.query(
            """SELECT a.id,a.content_fingerprint,a.content_digest,
                      CASE WHEN a.content_fingerprint='' THEN a.extracted_text ELSE '' END AS extracted_text,
                      CASE WHEN a.content_fingerprint='' THEN a.extraction_metadata_json ELSE '' END AS extraction_metadata_json
               FROM attachments a JOIN messages m ON m.id=a.message_id
               WHERE m.thread_id=? ORDER BY a.id""", (thread_id,),
        ):
            fingerprint = attachment["content_fingerprint"] or hashlib.sha256(
                "\0".join(str(attachment[key] or "") for key in
                    ("content_digest", "extracted_text", "extraction_metadata_json")).encode()).hexdigest()
            hasher.update(f"{attachment['id']}:{fingerprint}".encode())
            if not attachment["content_fingerprint"]:
                missing_attachment_fingerprints.append((fingerprint, attachment["id"]))
        digest = hasher.hexdigest()
        with self.db.transaction() as connection:
            connection.executemany("UPDATE messages SET content_fingerprint=? WHERE id=? AND content_fingerprint=''", missing_message_fingerprints)
            connection.executemany("UPDATE attachments SET content_fingerprint=? WHERE id=? AND content_fingerprint=''", missing_attachment_fingerprints)
            connection.execute("UPDATE threads SET message_count=?, content_hash=? WHERE id=?", (len(rows), digest, thread_id))
            connection.execute("DELETE FROM summaries WHERE thread_id=? AND content_hash != ?", (thread_id, digest))

    def _upsert_tone_example(
        self, account_id: str, message_id: str, recipients: dict[str, list[str]], body_text: str
    ) -> None:
        clean = clean_sent_body(body_text)
        reason = None
        if len(clean) < 40:
            reason = "too_short"
        if re.search(r"unsubscribe|no[- ]?reply|automated message", clean, re.IGNORECASE):
            reason = "automated_or_bulk"
        addresses = recipients.get("to", [])
        domain = addresses[0].split("@", 1)[1].lower() if addresses and "@" in addresses[0] else ""
        self.db.execute(
            """
            INSERT INTO tone_examples(message_id,account_id,recipient_domain,clean_body,excluded_reason,created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(message_id) DO UPDATE SET clean_body=excluded.clean_body,
              recipient_domain=excluded.recipient_domain,excluded_reason=excluded.excluded_reason
            """,
            (message_id, account_id, domain, clean, reason, self.db.now()),
        )

    def _match_sent_draft(self, account_id: str, message_id: str) -> None:
        sent = self.db.connection.execute(
            "SELECT sent_at,subject,body_text,recipients_json FROM messages WHERE id=? AND account_id=?",
            (message_id, account_id),
        ).fetchone()
        if not sent:
            return
        recipients = json.loads(sent["recipients_json"] or "{}")
        # A provider can rethread a sent reply, and another message can be
        # sent into the original thread after a draft was saved. Thread and
        # time alone cannot identify the human-approved version. Use the
        # reviewed content and addresses, regardless of provider thread, and
        # refuse ambiguous or legacy records without these fields.
        def addresses(values: Any) -> tuple[str, ...]:
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                return ()
            return tuple(sorted({email.casefold() for _, email in getaddresses(values) if email}))

        sent_to = addresses(recipients.get("to"))
        sent_cc = addresses(recipients.get("cc"))
        sent_body = normalize_message_text(sent["body_text"]).strip()
        if not sent_to or not sent_body or addresses(recipients.get("bcc")):
            return
        matches = []
        candidates = self.db.query(
            """SELECT id,selected_candidate_json FROM draft_records
               WHERE account_id=? AND matched_sent_message_id IS NULL
               AND julianday(created_at) <= julianday(?)""",
            (account_id, sent["sent_at"]),
        )
        for candidate in candidates:
            try:
                reviewed = json.loads(candidate["selected_candidate_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(reviewed, dict) or not {
                "subject", "body", "to", "cc", "bcc"
            }.issubset(reviewed) or reviewed["bcc"]:
                continue
            if (
                str(reviewed["subject"]).strip().casefold()
                == sent["subject"].strip().casefold()
                and normalize_message_text(str(reviewed["body"])).strip() == sent_body
                and addresses(reviewed["to"]) == sent_to
                and addresses(reviewed["cc"]) == sent_cc
            ):
                matches.append(candidate)
        if len(matches) != 1:
            return
        row = matches[0]
        note_scopes = {""}
        note_scopes.update(
            email.casefold() for _, email in getaddresses(
                recipients.get("to", []) + recipients.get("cc", [])
            ) if email
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE draft_records SET matched_sent_message_id=? WHERE id=?",
                (message_id, row["id"]),
            )
            connection.execute(
                "UPDATE tone_examples SET approved_sent_version=1 WHERE message_id=?",
                (message_id,),
            )
            connection.executemany(
                "DELETE FROM tone_notes WHERE account_id=? AND recipient=?",
                ((account_id, scope) for scope in note_scopes),
            )

    def mark_remote_deleted(self, account_id: str, provider_ids: list[str]) -> None:
        if not provider_ids:
            return
        placeholders = ",".join("?" for _ in provider_ids)
        parameters = [account_id, *provider_ids]
        self.db.execute(
            f"UPDATE messages SET source_state='remote_deleted' WHERE account_id=? AND provider_id IN ({placeholders})",
            parameters,
        )
        self.db.execute(f"DELETE FROM source_retry_queue WHERE account_id=? AND provider_id IN ({placeholders})", parameters)
        self.db.execute(
            """UPDATE threads SET source_state='remote_deleted' WHERE account_id=? AND NOT EXISTS(
                 SELECT 1 FROM messages WHERE messages.thread_id=threads.id AND messages.source_state='available'
               )""",
            (account_id,),
        )

    def begin_main_inbox_scope_scan(self, account_id: str, *, resume: bool) -> None:
        if not resume:
            self.db.execute("DELETE FROM main_inbox_scan_items WHERE account_id=?", (account_id,))

    def main_inbox_scan_count(self, account_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM main_inbox_scan_items WHERE account_id=?",
                (account_id,),
                0,
            )
        )

    def capture_main_inbox_page(self, account_id: str, messages: list[MessagePayload]) -> None:
        provider_ids = [message.provider_id for message in messages if "INBOX" in message.labels]
        if not provider_ids:
            return
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO main_inbox_scan_items(account_id,provider_id) VALUES(?,?)",
                ((account_id, provider_id) for provider_id in provider_ids),
            )

    def capture_main_inbox_ids(self, account_id: str, provider_ids: list[str]) -> None:
        if not provider_ids:
            return
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO main_inbox_scan_items(account_id,provider_id) VALUES(?,?)",
                ((account_id, provider_id) for provider_id in provider_ids),
            )

    def reconcile_main_inbox_scope(self, account_id: str, since: datetime | None) -> None:
        """Hide missing Inbox IDs after a complete provider scan; keep originals."""
        parameters: list[str] = [account_id]
        date_clause = ""
        if since:
            date_clause = " AND received_at>=?"
            parameters.append(since.astimezone(UTC).isoformat())
        self.db.execute(
            "UPDATE messages SET in_main_inbox=0 WHERE account_id=? "
            "AND in_main_inbox=1 AND is_sent=0 AND is_draft=0"
            + date_clause
            + " AND provider_id NOT IN ("
            "SELECT provider_id FROM main_inbox_scan_items WHERE account_id=?)",
            [*parameters, account_id],
        )

    def apply_main_inbox_scope(self, account_id: str, provider_ids: set[str] | None = None) -> int:
        """Hide out-of-scope mail without deleting its encrypted local copy."""
        if provider_ids:
            with self.db.transaction() as connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO main_inbox_scan_items(account_id,provider_id) VALUES(?,?)",
                    ((account_id, provider_id) for provider_id in provider_ids),
                )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE messages SET in_main_inbox=0 WHERE account_id=?", (account_id,)
            )
            connection.execute(
                """UPDATE messages SET in_main_inbox=1,is_filtered_bulk=0
                   WHERE account_id=? AND provider_id IN (
                     SELECT provider_id FROM main_inbox_scan_items WHERE account_id=?
                   )""",
                (account_id, account_id),
            )
            visible = connection.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE account_id=? AND in_main_inbox=1 AND is_filtered_bulk=0""",
                (account_id,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM main_inbox_scan_items WHERE account_id=?", (account_id,)
            )
        return int(visible)

    @staticmethod
    def _mailbox_predicate(mailbox: str, category: str = "all") -> tuple[str, list[Any]]:
        available = "source_state='available' AND is_filtered_bulk=0"
        if mailbox == "sent":
            return f"{available} AND is_sent=1 AND is_draft=0", []
        if mailbox == "drafts":
            return f"{available} AND is_draft=1", []
        if mailbox == "all":
            return (
                f"{available} AND (in_main_inbox=1 OR is_sent=1 OR is_draft=1)",
                [],
            )
        if mailbox == "important":
            return f"{available} AND (in_main_inbox=1 OR is_sent=1)", []
        predicate = f"{available} AND in_main_inbox=1"
        parameters: list[Any] = []
        if category in {"primary", "updates"}:
            predicate += " AND mail_category=?"
            parameters.append(category)
        return predicate, parameters

    def mailbox_counts(self, account_id: str | None = None) -> dict[str, int]:
        account_where = " AND m.account_id=?" if account_id else ""
        parameters: list[Any] = [account_id] if account_id else []
        row = self.db.connection.execute(
            f"""
            SELECT
              COUNT(DISTINCT CASE WHEN m.in_main_inbox=1 AND m.is_filtered_bulk=0
                    AND m.source_state='available' THEN m.thread_id END) AS inbox,
              COUNT(DISTINCT CASE WHEN m.in_main_inbox=1 AND m.mail_category='primary'
                    AND m.is_filtered_bulk=0 AND m.source_state='available' THEN m.thread_id END) AS primary_count,
              COUNT(DISTINCT CASE WHEN m.in_main_inbox=1 AND m.mail_category='updates'
                    AND m.is_filtered_bulk=0 AND m.source_state='available' THEN m.thread_id END) AS updates_count,
              COUNT(DISTINCT CASE WHEN m.is_sent=1 AND m.is_draft=0
                    AND m.source_state='available' THEN m.thread_id END) AS sent,
              COUNT(DISTINCT CASE WHEN m.is_draft=1 AND m.source_state='available'
                    THEN m.thread_id END) AS drafts,
              COUNT(DISTINCT CASE WHEN t.important_override=1 AND m.source_state='available'
                    AND (m.in_main_inbox=1 OR m.is_sent=1) THEN m.thread_id END) AS important
            FROM messages m JOIN threads t ON t.id=m.thread_id
            WHERE t.trashed_at IS NULL{account_where}
            """,
            parameters,
        ).fetchone()
        keys = ("inbox", "primary_count", "updates_count", "sent", "drafts", "important")
        return {key: int(row[key] or 0) for key in keys}

    def list_threads(
        self,
        account_id: str | None = None,
        limit: int = 200,
        *,
        mailbox: str = "inbox",
        category: str = "all",
        thread_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        predicate, parameters = self._mailbox_predicate(mailbox, category)
        if thread_ids is not None:
            if not thread_ids:
                return []
            # Retrieve the actual matching conversations, not a recent global
            # page that silently drops old results. Retained local sources count.
            # Explicit search results may include archived or filtered mail;
            # their source cards must still open the matching conversation.
            predicate = "thread_id IN (" + ",".join("?" for _ in thread_ids) + ")"
            parameters = list(thread_ids)
        where = "WHERE t.trashed_at IS NULL"
        if mailbox == "important":
            where += " AND t.important_override=1"
        if account_id:
            # Scope before ranking; account switches should not scan every mailbox.
            predicate += " AND account_id=?"
            parameters.append(account_id)
        parameters.append(limit)
        rows = self.db.query(
            f"""
            WITH ranked_messages AS (
              SELECT id,thread_id,received_at,
                ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY received_at DESC,id DESC) AS position,
                MAX(COALESCE(local_unread,is_unread)) OVER (PARTITION BY thread_id) AS thread_unread
              FROM messages WHERE {predicate}
            ), selected_threads AS (
              SELECT t.*,r.id AS latest_message_id,r.received_at AS view_latest_at,
                     r.thread_unread AS view_is_unread
              FROM threads t JOIN ranked_messages r ON r.thread_id=t.id AND r.position=1
              {where}
              ORDER BY r.received_at DESC LIMIT ?
            )
            SELECT t.*,a.email AS account_email,a.provider,a.color,
              vm.sender_name AS latest_sender,vm.sender_email AS latest_sender_email,
              vm.recipients_json AS latest_recipients_json,vm.snippet
            FROM selected_threads t JOIN accounts a ON a.id=t.account_id
            JOIN messages vm ON vm.id=t.latest_message_id
            ORDER BY t.view_latest_at DESC
            """,
            parameters,
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["latest_at"] = item.pop("view_latest_at")
            item["is_unread"] = item.pop("view_is_unread")
            item["snippet"] = preview_snippet(item["snippet"])
            recipients = json.loads(item.pop("latest_recipients_json") or "{}")
            if mailbox in {"sent", "drafts"}:
                addresses = recipients.get("to", [])
                item["latest_sender"] = (
                    "To: " + ", ".join(addresses) if addresses else "No recipient"
                )
            elif not item["latest_sender"]:
                item["latest_sender"] = item.pop("latest_sender_email", "")
            else:
                item.pop("latest_sender_email", None)
            output.append(item)
        return output

    def search_header_thread_ids(self, query: str, account_id: str | None = None, limit: int = 50) -> list[str]:
        """Local subject/address matches, including emails without body chunks."""
        fts = safe_fts_query(query)
        if not fts:
            return []
        scope = "AND m.account_id=?" if account_id else ""
        rows = self.db.query(
            f"""SELECT m.thread_id,MAX(m.received_at) newest FROM message_header_fts h
                JOIN messages m ON m.id=h.message_id JOIN threads t ON t.id=m.thread_id
                WHERE message_header_fts MATCH ? AND t.trashed_at IS NULL {scope}
                GROUP BY m.thread_id ORDER BY newest DESC LIMIT ?""",
            [fts, *([account_id] if account_id else []), limit],
        )
        # Old installations remain complete while their index is built in batches.
        # Once maintenance completes, no concatenated metadata scan is needed.
        chinese = bool(re.search(r"[\u3400-\u9fff]", query))
        if not chinese and self.db.scalar("SELECT value FROM app_settings WHERE key='local_index_version'", default="") == "v1":
            return [row["thread_id"] for row in rows]
        terms = list(dict.fromkeys(re.findall(r"[\w@.+-]+", query.casefold())))[:8]
        header = "lower(m.subject || ' ' || m.sender_name || ' ' || m.sender_email || ' ' || m.recipients_json)"
        predicate = " AND ".join(f"instr({header},?)>0" for _ in terms) or "0"
        missing_only = "" if chinese else "AND NOT EXISTS(SELECT 1 FROM message_header_fts h WHERE h.rowid=m.rowid)"
        older = self.db.query(
            f"""SELECT m.thread_id,MAX(m.received_at) newest FROM messages m
                JOIN threads t ON t.id=m.thread_id
                WHERE t.trashed_at IS NULL AND {predicate} {scope}
                  {missing_only}
                GROUP BY m.thread_id ORDER BY newest DESC LIMIT ?""",
            [*terms, *([account_id] if account_id else []), limit],
        )
        return list(dict.fromkeys(row["thread_id"] for row in
            sorted([*rows, *older], key=lambda row: row["newest"], reverse=True)))[:limit]

    def set_thread_unread(self, thread_id: str, unread: bool) -> None:
        """Device-local override: provider state and labels remain untouched."""
        self.db.execute(
            "UPDATE messages SET local_unread=? WHERE thread_id=?", (int(unread), thread_id)
        )

    def unread_counts(self, account_id: str | None = None) -> dict[str, int]:
        account_clause = " AND m.account_id=?" if account_id else ""
        rows = self.db.query(
            f"""SELECT m.mail_category,COUNT(DISTINCT m.thread_id) AS count
                FROM messages m JOIN threads t ON t.id=m.thread_id
                WHERE t.trashed_at IS NULL AND m.in_main_inbox=1
                  AND m.is_filtered_bulk=0 AND m.source_state='available'
                  AND COALESCE(m.local_unread,m.is_unread)=1 {account_clause}
                GROUP BY m.mail_category""",
            [account_id] if account_id else [],
        )
        counts = {row["mail_category"]: int(row["count"]) for row in rows}
        # Threads can have messages in multiple categories: count inbox separately.
        counts["inbox"] = self.db.scalar(
            f"""SELECT COUNT(DISTINCT m.thread_id) FROM messages m JOIN threads t ON t.id=m.thread_id
                WHERE t.trashed_at IS NULL AND m.in_main_inbox=1 AND m.is_filtered_bulk=0
                  AND m.source_state='available' AND COALESCE(m.local_unread,m.is_unread)=1
                  {account_clause}""",
            [account_id] if account_id else [],
            0,
        )
        return counts

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self.db.read_snapshot():
            return self._get_thread_full(thread_id)

    def _get_thread_full(self, thread_id: str) -> dict[str, Any] | None:
        rows = self.db.query(
            """SELECT t.*,a.email AS account_email,a.provider,a.color
               FROM threads t JOIN accounts a ON a.id=t.account_id WHERE t.id=?""",
            (thread_id,),
        )
        if not rows:
            return None
        result = dict(rows[0])
        messages = self.db.query(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY sent_at", (thread_id,)
        )
        result["messages"] = []
        for message in messages:
            item = dict(message)
            item["is_unread"] = (
                item["is_unread"] if item["local_unread"] is None else item["local_unread"]
            )
            item["recipients"] = json.loads(item.pop("recipients_json"))
            item["labels"] = json.loads(item.pop("labels_json"))
            # Drafts are editable user work. Even when Mail serializes the
            # entire body inside a blockquote, none of it is expendable
            # quoted history in the reading view.
            item["new_content"] = (
                item["body_text"] if item["is_draft"]
                else message_new_content(item["body_text"], item["body_html"])
            )
            item["has_quoted_history"] = bool(
                not item["is_draft"]
                and item["new_content"] != normalize_message_text(item["body_text"])
            )
            item["attachments"] = [
                dict(a)
                for a in self.db.query(
                    "SELECT * FROM attachments WHERE message_id=?", (item["id"],)
                )
            ]
            result["messages"].append(item)
        result["is_unread"] = int(any(item["is_unread"] for item in result["messages"]))
        return result

    def get_thread_for_read(self, thread_id: str, message_id: str | None = None) -> dict | None:
        """Metadata-only older messages plus one prepared body, in chronological order."""
        with self.db.read_snapshot():
            rows = self.db.query(
                """SELECT t.*,a.email AS account_email,a.provider,a.color FROM threads t
                   JOIN accounts a ON a.id=t.account_id WHERE t.id=?""", (thread_id,),
            )
            if not rows:
                return None
            result = dict(rows[0])
            result["messages"] = [dict(row) for row in self.db.query(
                """SELECT id,thread_id,account_id,provider_id,provider_revision,subject,sender_name,sender_email,sent_at,
                          received_at,recipients_json,labels_json,is_sent,is_draft,
                          COALESCE(local_unread,is_unread) AS is_unread,source_complete,source_state,web_link,
                          internet_message_id,in_reply_to,raw_digest
                   FROM messages WHERE thread_id=? ORDER BY sent_at,id""", (thread_id,),
            )]
            selected = message_id or (result["messages"][-1]["id"] if result["messages"] else "")
            for item in result["messages"]:
                item["recipients"] = json.loads(item.pop("recipients_json"))
                item["labels"] = json.loads(item.pop("labels_json"))
                item.update(attachments=[], prepared=False, body_text="", body_html="",
                            new_content="", has_quoted_history=False)
                if item["id"] == selected:
                    item.update(self.prepare_message_for_read(item["id"]) or {})
            result["is_unread"] = int(any(item["is_unread"] for item in result["messages"]))
            return result

    def prepare_message_for_read(self, message_id: str) -> dict | None:
        with self.db.read_snapshot():
            rows = self.db.query("SELECT * FROM messages WHERE id=?", (message_id,))
            if not rows:
                return None
            item = dict(rows[0])
            item["is_unread"] = item["is_unread"] if item["local_unread"] is None else item["local_unread"]
            item["recipients"] = json.loads(item.pop("recipients_json"))
            item["labels"] = json.loads(item.pop("labels_json"))
            item["new_content"] = item["body_text"] if item["is_draft"] else message_new_content(item["body_text"], item["body_html"])
            item["has_quoted_history"] = bool(not item["is_draft"] and item["new_content"] != normalize_message_text(item["body_text"]))
            item["attachments"] = [dict(row) for row in self.db.query(
                """SELECT id,message_id,provider_id,filename,mime_type,byte_size,content_digest,
                          extraction_metadata_json,is_inline,content_id,content_location,created_at
                   FROM attachments WHERE message_id=?""", (message_id,),
            )]
            item["prepared"] = True
            return item

    def prepare_reply_thread(self, thread: dict[str, Any]) -> dict[str, Any]:
        """Backfill legacy reply headers only when a reply is requested."""
        candidates = [item for item in thread.get("messages", []) if not item.get("is_draft")]
        if not candidates:
            return thread
        latest = max(candidates, key=lambda item: item["sent_at"])
        recipients = latest["recipients"]
        if "reply_to" not in recipients and latest.get("raw_digest"):
            recipients = {**recipients, **reply_headers(self.blobs.get(latest["raw_digest"]))}
            self.db.execute("UPDATE messages SET recipients_json=? WHERE id=?",
                            (json.dumps(recipients), latest["id"]))
            latest["recipients"] = recipients
        return thread

    def get_tone_examples(
        self, account_id: str, recipient: str = "", limit: int = 6
    ) -> list[dict[str, Any]]:
        recipient = parseaddr(recipient)[1].casefold()
        # Exact-recipient metadata is indexed at ingestion/backfill. Never load
        # and clean the entire sent history merely to return six examples.
        exact_available = bool(recipient and self.db.scalar(
            """SELECT 1 FROM tone_recipients tr JOIN tone_examples te ON te.message_id=tr.message_id
               JOIN messages m ON m.id=te.message_id JOIN threads t ON t.id=m.thread_id
               WHERE tr.email=? AND te.account_id=? AND te.excluded_reason IS NULL
                 AND t.trashed_at IS NULL LIMIT 1""", (recipient, account_id),
        ))
        recipient_filter = "AND EXISTS(SELECT 1 FROM tone_recipients tr WHERE tr.message_id=m.id AND tr.email=?)" if exact_available else ""
        rows = [
            dict(row)
            for row in self.db.query(
                f"""SELECT te.*,m.subject,m.sent_at,m.recipients_json FROM tone_examples te
                   JOIN messages m ON m.id=te.message_id
                   JOIN threads t ON t.id=m.thread_id
                   WHERE te.account_id=? AND te.excluded_reason IS NULL AND t.trashed_at IS NULL
                   {recipient_filter}
                   ORDER BY te.approved_sent_version DESC,m.sent_at DESC LIMIT ?""",
                (account_id, *([recipient] if exact_available else []), limit if exact_available else 200),
            )
        ]
        for row in rows:
            addresses = json.loads(row.pop("recipients_json"))
            row["exact_recipient"] = recipient in {
                email.casefold()
                for _, email in getaddresses(addresses.get("to", []) + addresses.get("cc", []))
            }
            row["clean_body"] = clean_sent_body(row["clean_body"])[:4000]
        exact = [row for row in rows if row["exact_recipient"]]
        return (exact or rows)[:limit]

    def cached_tone_note(self, account_id: str, recipient: str) -> dict | None:
        recipient = parseaddr(recipient)[1].casefold()
        row = self.db.connection.execute(
            "SELECT * FROM tone_notes WHERE account_id=? AND recipient=?",
            (account_id, recipient),
        ).fetchone()
        if not row or datetime.fromisoformat(row["updated_at"]) < datetime.now(UTC) - timedelta(
            days=30
        ):
            return None
        return {
            "document": row["document"],
            "tone_example_ids": json.loads(row["example_ids_json"]),
            "updated_at": row["updated_at"],
            "recipient": recipient,
        }

    def save_tone_note(
        self, account_id: str, recipient: str, document: str, example_ids: list[str]
    ) -> dict:
        recipient = parseaddr(recipient)[1].casefold()
        self.db.execute(
            """INSERT INTO tone_notes VALUES(?,?,?,?,?) ON CONFLICT(account_id,recipient)
               DO UPDATE SET document=excluded.document,example_ids_json=excluded.example_ids_json,
               updated_at=excluded.updated_at""",
            (account_id, recipient, document, json.dumps(example_ids), self.db.now()),
        )
        return self.cached_tone_note(account_id, recipient)

    def recipient_suggestions(
        self, account_id: str, query: str, limit: int = 8
    ) -> list[dict[str, str]]:
        query = query.strip().casefold()
        return [{"email": row["email"], "name": row["name"],
                 "label": f"{row['name']} <{row['email']}>" if row["name"] else row["email"]}
                for row in self.db.query(
                    """SELECT email,name FROM contacts c WHERE account_id=?
                       AND instr(lower(name || ' ' || email),?)>0 AND EXISTS(
                         SELECT 1 FROM contact_sources s JOIN messages m ON m.id=s.message_id
                         JOIN threads t ON t.id=m.thread_id WHERE s.account_id=c.account_id
                           AND s.email=c.email AND t.trashed_at IS NULL)
                       ORDER BY last_seen DESC LIMIT ?""",
                    (account_id, query, limit),
                )]

    def _update_contacts(self, account_id, message_id, sender_name, sender_email, recipients, is_sent, stamp):
        owner = self.db.scalar("SELECT email FROM accounts WHERE id=?", (account_id,), "").casefold()
        addresses = recipients.get("to", []) + recipients.get("cc", [])
        if is_sent:
            addresses += recipients.get("bcc", [])
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM contact_sources WHERE message_id=?", (message_id,))
            for name, email in [(sender_name, sender_email), *getaddresses(addresses)]:
                email = email.casefold().strip()
                if (email == owner or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email)
                        or re.search(r"no[-_.]?reply|do[-_.]?not[-_.]?reply", email)):
                    continue
                connection.execute(
                    """INSERT INTO contacts(account_id,email,name,last_seen) VALUES(?,?,?,?)
                       ON CONFLICT(account_id,email) DO UPDATE SET
                         name=CASE WHEN contacts.name='' THEN excluded.name ELSE contacts.name END,
                         last_seen=MAX(contacts.last_seen,excluded.last_seen)""",
                    (account_id, email, name, stamp),
                )
                connection.execute("INSERT OR IGNORE INTO contact_sources(account_id,email,message_id) VALUES(?,?,?)",
                                   (account_id, email, message_id))
            connection.execute("DELETE FROM tone_recipients WHERE message_id=?", (message_id,))
            if is_sent:
                connection.executemany(
                    "INSERT OR IGNORE INTO tone_recipients(message_id,email) VALUES(?,?)",
                    [(message_id, email.casefold()) for _, email in
                     getaddresses(recipients.get("to", []) + recipients.get("cc", [])) if email],
                )

    def maintain_local_indexes(self, limit: int = 200) -> dict[str, int]:
        """Resumable metadata-only legacy backfill, never needed on the GUI thread."""
        if self.db.scalar("SELECT value FROM app_settings WHERE key='local_index_version'", default="") == "v1":
            return {"updated": 0, "remaining": 0}
        offset = int(self.db.scalar("SELECT value FROM app_settings WHERE key='local_index_rowid'", default="0"))
        rows = self.db.query(
            """SELECT rowid,id,account_id,subject,sender_name,sender_email,recipients_json,is_sent,sent_at
               FROM messages WHERE rowid>? ORDER BY rowid LIMIT ?""", (offset, limit),
        )
        with self.db.transaction() as connection:
            for row in rows:
                connection.execute(
                    """INSERT INTO message_header_fts(rowid,message_id,subject,sender_name,sender_email,recipients_json)
                       SELECT ?,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM message_header_fts WHERE rowid=?)""",
                    (row["rowid"], row["id"], row["subject"], row["sender_name"], row["sender_email"], row["recipients_json"], row["rowid"]),
                )
                self._update_contacts(row["account_id"], row["id"], row["sender_name"], row["sender_email"],
                                      json.loads(row["recipients_json"]), row["is_sent"], row["sent_at"])
            if rows:
                connection.execute("INSERT INTO app_settings(key,value) VALUES('local_index_rowid',?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(rows[-1]["rowid"]),))
            if len(rows) < limit:
                connection.execute("INSERT INTO app_settings(key,value) VALUES('local_index_version','v1') "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        return {"updated": len(rows), "remaining": int(len(rows) == limit)}

    def save_summary(
        self, thread_id: str, content_hash: str, summary_json: str, model: str
    ) -> None:
        self.db.execute(
            """INSERT INTO summaries(thread_id,content_hash,summary_json,model,created_at)
               VALUES(?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET
               content_hash=excluded.content_hash,summary_json=excluded.summary_json,
               model=excluded.model,created_at=excluded.created_at""",
            (thread_id, content_hash, summary_json, model, self.db.now()),
        )

    def cached_summary(
        self, thread_id: str, content_hash: str, cache_model: str | None = None
    ) -> str | None:
        return self.db.scalar(
            "SELECT summary_json FROM summaries WHERE thread_id=? AND content_hash=? "
            "AND (? IS NULL OR model=?)",
            (thread_id, content_hash, cache_model, cache_model),
        )

    def save_facts(self, message_id: str, facts: list[ExtractedFact]) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM extracted_facts WHERE message_id=?", (message_id,))
            for fact in facts:
                connection.execute(
                    """INSERT INTO extracted_facts(
                       id,message_id,fact_type,value,normalized_value,source_excerpt,confidence,important,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        stable_id(message_id, "fact", fact.fact_type, fact.value),
                        message_id,
                        fact.fact_type,
                        fact.value,
                        fact.normalized_value,
                        fact.source_excerpt,
                        fact.confidence,
                        int(fact.important),
                        self.db.now(),
                    ),
                )

    def override_fact(self, fact_id: str, *, value: str | None, important: bool | None) -> None:
        self.db.execute(
            "UPDATE extracted_facts SET override_value=?,override_important=? WHERE id=?",
            (value, None if important is None else int(important), fact_id),
        )

    def set_thread_important(self, thread_id: str, value: bool | None) -> None:
        self.db.execute(
            "UPDATE threads SET important_override=? WHERE id=?",
            (None if value is None else int(value), thread_id),
        )

    def trash_thread(self, thread_id: str) -> None:
        self._contact_cache.clear()
        self.db.execute("UPDATE threads SET trashed_at=? WHERE id=?", (self.db.now(), thread_id))

    def purge_expired_trash(self, days: int = 30) -> int:
        threshold = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.db.query(
            "SELECT id FROM threads WHERE trashed_at IS NOT NULL AND trashed_at<?", (threshold,)
        )
        for row in rows:
            self._purge_thread(row["id"])
        return len(rows)

    def _purge_thread(self, thread_id: str) -> None:
        digests = [
            row["content_digest"]
            for row in self.db.query(
                """SELECT a.content_digest FROM attachments a JOIN messages m ON m.id=a.message_id
                   WHERE m.thread_id=? AND a.content_digest IS NOT NULL""",
                (thread_id,),
            )
        ]
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM source_retry_queue WHERE EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=? AND m.account_id=source_retry_queue.account_id AND m.provider_id=source_retry_queue.provider_id)", (thread_id,))
            connection.execute(
                "DELETE FROM tone_notes WHERE account_id=(SELECT account_id FROM threads WHERE id=?)",
                (thread_id,),
            )
            message_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM messages WHERE thread_id=?", (thread_id,)
                )
            ]
            for message_id in message_ids:
                connection.execute("DELETE FROM extracted_facts WHERE message_id=?", (message_id,))
                connection.execute("DELETE FROM tone_examples WHERE message_id=?", (message_id,))
                connection.execute("DELETE FROM attachments WHERE message_id=?", (message_id,))
            connection.execute("DELETE FROM source_chunks WHERE thread_id=?", (thread_id,))
            connection.execute("DELETE FROM summaries WHERE thread_id=?", (thread_id,))
            connection.execute("DELETE FROM draft_records WHERE thread_id=?", (thread_id,))
            connection.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
            connection.execute("DELETE FROM threads WHERE id=?", (thread_id,))
        for digest in digests:
            remaining = self.db.scalar(
                "SELECT count(*) FROM attachments WHERE content_digest=?", (digest,), 0
            )
            if not remaining:
                self.blobs.delete(digest)

    def answer_file_cards(
        self, primary_ids: list[str], related_ids: list[str], *, question: str = ""
    ) -> list[dict]:
        """Order confirmed files with available same-message PDF companions.

        A model may cite an ID card yet omit the binder attached to the same
        email. Companions are visibly marked possible, not promoted to cited
        evidence, and never cross account, message, or app-trash boundaries.
        """
        primary = list(dict.fromkeys(primary_ids))
        primary_set = set(primary)
        ordered: list[dict] = []
        seen: set[str] = set()
        companion_count = 0

        def add(attachment_id: str) -> dict | None:
            if attachment_id in seen:
                return None
            row = self.db.connection.execute(
                f"""SELECT a.id,a.filename,a.mime_type,a.byte_size,a.message_id,
                          m.sent_at AS email_date,m.thread_id,m.web_link AS provider_link,
                          m.account_id,a.content_digest,
                          a.content_digest IS NOT NULL AS available
                   FROM attachments a JOIN messages m ON m.id=a.message_id
                   JOIN threads t ON t.id=m.thread_id
                   WHERE a.id=? AND t.trashed_at IS NULL AND {visible_mail_file_sql('a')}""",
                (attachment_id,),
            ).fetchone()
            if not row:
                return None
            card = dict(row)
            card["document_dates"] = printed_document_dates([dict(chunk) for chunk in self.db.query(
                "SELECT id,content,locator FROM source_chunks "
                "WHERE source_type='attachment' AND source_id=? AND chunk_index>=0 "
                "ORDER BY chunk_index", (attachment_id,),
            )])
            card["possible_match"] = attachment_id not in primary_set
            seen.add(attachment_id)
            ordered.append(card)
            return card

        for attachment_id in primary:
            card = add(attachment_id)
            if not card or card["mime_type"] != "application/pdf" or companion_count >= 2:
                continue
            siblings = self.db.query(
                """SELECT id FROM attachments
                   WHERE message_id=? AND id<>?
                     AND mime_type='application/pdf' AND content_digest IS NOT NULL
                   ORDER BY filename LIMIT ?""",
                (card["message_id"], attachment_id, 2 - companion_count),
            )
            for sibling in siblings:
                if add(sibling["id"]):
                    companion_count += 1
        for attachment_id in related_ids:
            add(attachment_id)
        # The same original can arrive under a generic and a descriptive name
        # in separate messages. Exact encrypted-blob checksums, not similar text
        # or filenames, permit one card while retaining every source relationship.
        groups: dict[tuple, list[dict]] = {}
        for card in ordered:
            key = (card["account_id"], card["mime_type"],
                   card["content_digest"] or card["id"])
            groups.setdefault(key, []).append(card)
        cards = []
        generic = {"binder", "temporary", "auto", "id", "card", "insurance", "policy",
                   "quote", "document", "copy", "attachment", "scan", "invoice", "receipt",
                   "contract", "certificate", "personal"}

        def specificity(card: dict) -> tuple[int, int]:
            tokens = re.findall(r"[\w]+", Path(card["filename"]).stem.lower())
            named = {token for token in tokens if token not in generic and not token.isdigit()}
            return len(named), sum(len(token) for token in named)

        def normalized(value: str) -> str:
            return " ".join(re.findall(r"[^\W_]+", value.lower()))

        def explicitly_requested(card: dict) -> bool:
            query = " " + normalized(question) + " "
            filename = normalized(card["filename"])
            stem = normalized(Path(card["filename"]).stem)
            return (
                " " + filename + " " in query
                or (specificity(card)[0] > 0 and " " + stem + " " in query)
            )

        for originals in groups.values():
            canonical = max(originals, key=lambda card: (
                explicitly_requested(card), specificity(card)
            ))
            canonical["duplicate_sources"] = [
                {name: original[name] for name in (
                    "id", "filename", "message_id", "thread_id", "email_date", "provider_link"
                )} | {"app_link": "emailreader://source/" + original["id"],
                     "cited": original["id"] in primary_set}
                for original in originals if original["id"] != canonical["id"]
            ]
            # Do not silently promote an uncited source to a model citation.
            canonical["identical_original_cited"] = (
                canonical["possible_match"] and any(
                    original["id"] in primary_set for original in originals
                )
            )
            cards.append(canonical)
        if question:
            # Rank recognized documents across the already-scoped retrieval,
            # not in frozen role/account/MIME slots. A useful binder must not
            # remain behind multiple quotes just because its original slot was
            # fourth. Ranking never merges identities or promotes citations.
            roles = {}
            for index, card in enumerate(cards):
                stem = normalized(Path(card["filename"]).stem)
                role = next((name for name in (
                    "id card", "binder", "policy", "quote", "invoice", "receipt",
                    "contract", "certificate",
                ) if re.search(r"\b" + name + r"\b", stem)), "")
                if role:
                    roles[index] = role
            query = normalized(question)
            requested_roles = {role for role in roles.values()
                               if re.search(r"\b" + role + r"s?\b", query)}
            proof_roles = {"id card", "binder", "policy", "contract", "certificate"}
            positions = list(roles)
            ranked = sorted(positions, key=lambda index: (
                explicitly_requested(cards[index]), bool(cards[index]["available"]),
                roles[index] in requested_roles, roles[index] in proof_roles,
                specificity(cards[index]),
            ), reverse=True)
            ordered_roles = [cards[index] for index in ranked]
            for index, card in zip(positions, ordered_roles, strict=True):
                cards[index] = card
        for card in cards:
            for name in ("account_id", "content_digest"):
                card.pop(name, None)
        return cards

    def exportable_attachment(self, attachment_id: str) -> dict | None:
        """Resolve an original only while its source remains outside app trash."""
        row = self.db.connection.execute(
            """SELECT a.filename,a.content_digest FROM attachments a
               JOIN messages m ON m.id=a.message_id
               JOIN threads t ON t.id=m.thread_id
               WHERE a.id=? AND t.trashed_at IS NULL""", (attachment_id,)
        ).fetchone()
        return dict(row) if row else None

    def export_attachment(self, attachment_id: str, destination: Path) -> Path:
        row = self.exportable_attachment(attachment_id)
        if not row or not row["content_digest"]:
            raise FileNotFoundError("Attachment content has not been downloaded")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = destination / Path(row["filename"]).name
        output.write_bytes(self.blobs.get(row["content_digest"]))
        output.chmod(0o600)
        return output

    def citation_for_chunk(self, chunk_id: str) -> Citation | None:
        row = self.db.connection.execute(
            """
            SELECT c.id,c.source_type,c.source_id,c.content,c.locator,a.email AS account_email,
                   t.subject,m.sender_email,m.sent_at,m.web_link,
                   CASE WHEN c.source_type='attachment' THEN att.filename ELSE t.subject END AS title
            FROM source_chunks c
            JOIN accounts a ON a.id=c.account_id JOIN threads t ON t.id=c.thread_id
            LEFT JOIN messages m ON m.id=CASE WHEN c.source_type='message' THEN c.source_id ELSE (SELECT message_id FROM attachments WHERE id=c.source_id) END
            LEFT JOIN attachments att ON att.id=CASE WHEN c.source_type='attachment' THEN c.source_id END
            WHERE c.id=?
            """,
            (chunk_id,),
        ).fetchone()
        if not row:
            return None
        return Citation(
            source_id=row["id"],
            source_type=row["source_type"],
            account_email=row["account_email"],
            title=row["title"],
            sender=row["sender_email"] or "",
            sent_at=row["sent_at"] or "",
            locator=row["locator"] or "",
            excerpt=row["content"][:400],
            app_link=f"emailreader://source/{row['id']}",
            provider_link=row["web_link"],
        )

    def resolve_source_link(self, identifier: str) -> tuple[str, str] | None:
        """Resolve indexed chunks and summary IDs to (thread, exact message)."""
        with self.db.read_snapshot():
            return self._resolve_source_link(identifier)

    def _resolve_source_link(self, identifier: str) -> tuple[str, str] | None:
        row = self.db.connection.execute(
            """SELECT c.thread_id,
                      CASE WHEN c.source_type='message' THEN c.source_id
                           ELSE a.message_id END AS message_id
               FROM source_chunks c
               LEFT JOIN attachments a ON a.id=c.source_id AND c.source_type='attachment'
               WHERE c.id=?""",
            (identifier,),
        ).fetchone()
        if not row:
            row = self.db.connection.execute(
                "SELECT thread_id,id AS message_id FROM messages WHERE id=?",
                (identifier,),
            ).fetchone()
        if not row:
            row = self.db.connection.execute(
                """SELECT m.thread_id,m.id AS message_id FROM attachments a
                   JOIN messages m ON m.id=a.message_id WHERE a.id=?""",
                (identifier,),
            ).fetchone()
        if not row or not row["message_id"]:
            return None
        visible = self.db.scalar(
            "SELECT 1 FROM threads WHERE id=? AND trashed_at IS NULL", (row["thread_id"],)
        )
        return (row["thread_id"], row["message_id"]) if visible else None
