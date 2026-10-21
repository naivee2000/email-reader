"""Explicit diagnostics: private mail/results stay in encrypted databases."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from bs4 import BeautifulSoup

from email_reader.ai import AssistantService, OpenAIService
from email_reader.database import Database
from email_reader.drafts import reply_requires_instruction, reply_unavailable
from email_reader.rendering import render_message
from email_reader.repository import MailRepository
from email_reader.search import HybridSearch
from email_reader.text import clean_sent_body


def sync_health(context) -> dict:
    """Read aggregate dates/counts only; never print headers, IDs, or tokens."""
    accounts = []
    since = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    for index, account in enumerate(context.repository.list_accounts(), 1):
        rows = context.database.query(
            """SELECT date(m.received_at,'localtime') day, m.mail_category category,
                      (m.in_main_inbox=1 AND m.source_state='available'
                       AND m.is_filtered_bulk=0 AND t.trashed_at IS NULL) visible, COUNT(*) count
               FROM messages m JOIN threads t ON t.id=m.thread_id
               WHERE m.account_id=? AND m.received_at>=?
               GROUP BY day,category,visible ORDER BY day DESC""", (account["id"], since),
        )
        accounts.append({
            "account_number": index,
            "bridge": "apple_mail" if account["id"].startswith("apple-mail:") else "api",
            "last_synced_at": account["last_synced_at"],
            "pending_import": bool(account["sync_page_token"]),
            "recent_message_counts": [dict(row) for row in rows],
        })
    return {"checked_at": datetime.now(UTC).isoformat(), "accounts": accounts}


def inspect_import(context, subject: str, *, recover: bool = False, account_number: int | None = None) -> dict:
    """Inspect (or explicitly repair) a bounded subject sample, without logging content."""
    from email import policy
    from email.parser import BytesParser

    from email_reader.categories import classify_inbox_headers
    from email_reader.providers.apple_mail import AppleMailProvider, cached_apple_mail_accounts

    accounts = context.repository.list_accounts()
    discovered, _ = cached_apple_mail_accounts(context.database, {
        a["email"] for a in accounts if a["id"].startswith("apple-mail:")
    })
    results = []
    for number, account in enumerate(accounts, 1):
        if account_number is not None and number != account_number:
            continue
        info = discovered.get(account["email"].casefold())
        if not info:
            continue
        provider = AppleMailProvider(account["id"], info)
        result = {"account_number": number, "matches": []}
        try:
            mailbox = next(m for m in provider._mailbox_names if provider._mailbox_role(m) == "inbox")
            batch = provider._fetch_mailbox_metadata(mailbox, 0, 80, datetime.now(UTC) - timedelta(days=14))
            for index, row in enumerate(batch.get("rows", [])):
                if subject.casefold() not in str(row.get("subject", "")).casefold():
                    continue
                identifier = (row.get("messageId") or row["internalId"]).strip("<>")
                sources = provider._fetch_mailbox_sources(mailbox, index, 1)
                if not sources or not sources[0]:
                    result["matches"].append({"original_available": False})
                    continue
                headers = BytesParser(policy=policy.default).parsebytes(sources[0].encode(), headersonly=True)
                if str(headers.get("Message-ID", "")).strip("<> ") != identifier:
                    raise RuntimeError("Mailbox moved during diagnostic read")
                match = {
                    "original_available": True,
                    "stored_locally": bool(context.database.scalar(
                        "SELECT COUNT(*) FROM messages WHERE account_id=? AND provider_id=?",
                        (account["id"], identifier),
                    )),
                    "list_id_present": bool(headers.get("List-ID")),
                    "unsubscribe_present": bool(headers.get("List-Unsubscribe")),
                    "bulk_or_list_precedence": str(headers.get("Precedence", "")).casefold() in {"bulk", "list"},
                    "current_category": classify_inbox_headers(headers),
                }
                if recover:
                    payload = provider._message({**row, "source": sources[0]}, mailbox)
                    if payload is not None:
                        context.repository.ingest_message(account["id"], payload)
                        match["recovered_locally"] = True
                    else:
                        match["recovered_locally"] = False
                result["matches"].append(match)
        except (RuntimeError, StopIteration):
            result["read_incomplete"] = True
        results.append(result)
    return {"bounded_to_first_80_recent_inbox_messages": True, "accounts": results}


def benchmark_read_open(context) -> dict:
    """Measure the real inbox's local read path on an encrypted throwaway copy.

    No provider is contacted and neither the live database nor provider read
    state is modified. Only aggregate timings and invariant flags leave here.
    """
    from email_reader.controller import AppController

    with tempfile.TemporaryDirectory(
        prefix="read-open-benchmark-", dir=context.paths.recovery
    ) as temporary:
        snapshot_path = Path(temporary) / "snapshot.db"
        copy = Database(
            snapshot_path, context.database.passphrase,
            allow_plaintext=context.database.allow_plaintext,
        )
        context.database.backup_to(snapshot_path)
        copy.open()
        controller = None
        try:
            repository = MailRepository(copy, context.repository.blobs)
            diagnostic_context = replace(context, database=copy, repository=repository)
            controller = AppController(diagnostic_context)
            account_ids = [account["id"] for account in controller.accounts if account["enabled"]]
            if len(account_ids) != 3:
                raise ValueError("Three enabled accounts are required")
            groups = {
                account_id: [thread for thread in controller.threads
                             if thread["account_id"] == account_id]
                for account_id in account_ids
            }
            if any(not group for group in groups.values()):
                raise ValueError("Each account needs a visible primary inbox conversation")
            threads = []
            while len(threads) < 10 and any(groups.values()):
                for account_id in account_ids:
                    if groups[account_id] and len(threads) < 10:
                        threads.append(groups[account_id].pop(0))
            if len(threads) != 10:
                raise ValueError("Ten visible primary inbox conversations are required")
            durations = []
            verified = True
            for thread in threads:
                thread_id = thread["id"]
                provider_flags = [tuple(row) for row in copy.query(
                    "SELECT id,is_unread FROM messages WHERE thread_id=? ORDER BY id",
                    (thread_id,),
                )]
                repository.set_thread_unread(thread_id, True)
                thread["is_unread"] = 1
                before = repository.unread_counts()["inbox"]
                started = time.perf_counter()
                controller.selectThread(thread_id)
                durations.append(time.perf_counter() - started)
                after = repository.unread_counts()["inbox"]
                verified &= (
                    before - after == 1
                    and controller.selectedThread.get("id") == thread_id
                    and thread["is_unread"] == 0
                    and provider_flags == [tuple(row) for row in copy.query(
                        "SELECT id,is_unread FROM messages WHERE thread_id=? ORDER BY id",
                        (thread_id,),
                    )]
                )
            return {
                "cases": len(durations),
                "accounts_represented": len({thread["account_id"] for thread in threads}),
                "median_seconds": round(statistics.median(durations), 4),
                "max_seconds": round(max(durations), 4),
                "p95_seconds": round(
                    sorted(durations)[math.ceil(len(durations) * 0.95) - 1], 4
                ),
                "all_under_one_second": all(value < 1 for value in durations),
                "local_read_and_provider_invariants": bool(verified),
                "measured_on_encrypted_copy": not copy.allow_plaintext,
            }
        finally:
            if controller is not None:
                controller.timer.stop()
            copy.close()


def freeze_rendering_reference_set(context) -> dict:
    """Persist a deterministic 12-thread visual cohort inside encrypted storage."""
    accounts = [row for row in context.repository.list_accounts() if row["enabled"]]
    if len(accounts) != 3:
        raise ValueError("Three enabled accounts are required")
    selected: list[str] = []
    per_account: dict[str, int] = {}
    reported = 0
    for account in accounts:
        rows = context.database.query(
            """SELECT t.id,t.subject,
                      MAX(CASE WHEN length(trim(m.body_html))>0 THEN 1 ELSE 0 END) has_html,
                      MAX(CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END) has_attachment,
                      MAX(CASE WHEN lower(COALESCE(a.mime_type,'')) LIKE 'image/%' THEN 1 ELSE 0 END) has_image,
                      MAX(t.latest_at) latest_at
               FROM threads t JOIN messages m ON m.thread_id=t.id
               LEFT JOIN attachments a ON a.message_id=m.id
               WHERE t.account_id=? AND t.trashed_at IS NULL
               GROUP BY t.id,t.subject
               ORDER BY (upper(trim(t.subject))='AUTO QUOTE') DESC,
                        has_image DESC,has_attachment DESC,has_html DESC,latest_at DESC""",
            (account["id"],),
        )
        chosen = rows[:4]
        if len(chosen) != 4:
            raise ValueError("Each account needs four stored conversations")
        selected.extend(row["id"] for row in chosen)
        per_account[account["id"]] = len(chosen)
        reported += sum(upper == "AUTO QUOTE" for upper in (
            str(row["subject"] or "").strip().upper() for row in chosen
        ))
    context.database.execute(
        "INSERT INTO app_settings(key,value) VALUES('rendering_reference_thread_ids',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(selected),),
    )
    return {
        "cases": len(selected),
        "accounts": len(per_account),
        "four_per_account": all(value == 4 for value in per_account.values()),
        "reported_auto_quote_included": bool(reported),
        "stored_encrypted": not context.database.allow_plaintext,
    }


def audit_search(context) -> dict:
    started = time.perf_counter()
    answer, hits = context.assistant.answer("auto insurance")
    # Keep generated text and source relationships in the encrypted store only.
    context.database.execute(
        "INSERT INTO app_settings(key,value) VALUES('insurance_search_audit',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(answer.model_dump(mode="json")),),
    )
    files = []
    for identifier in answer.attachment_ids:
        rows = context.database.query(
            "SELECT a.filename,m.sent_at FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.id=?",
            (identifier,),
        )
        files.extend({"filename": row["filename"], "email_date": row["sent_at"]} for row in rows)
    expected = {"2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf"}
    return {
        "seconds": round(time.perf_counter() - started, 2),
        "top_three_files": files[:3],
        "expected_files_in_top_three": expected.issubset({item["filename"] for item in files[:3]}),
        "expected_files_retrieved": sorted(expected.intersection({hit.citation.title for hit in hits})),
        "citation_count": len(answer.source_ids),
        "all_citations_resolve": bool(answer.source_ids) and set(answer.source_ids).issubset({hit.chunk_id for hit in hits}),
    }


def audit_sources(context) -> dict:
    filenames = ("2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf")
    report = {"accounts": len(context.repository.list_accounts()), "expected_files": []}
    for filename in filenames:
        rows = context.database.query(
            """SELECT a.*,m.thread_id,m.sent_at FROM attachments a JOIN messages m ON m.id=a.message_id
               WHERE lower(a.filename)=lower(?) ORDER BY m.sent_at DESC""",
            (filename,),
        )
        documents = []
        for row in rows:
            thread = context.repository.get_thread(row["thread_id"])
            embedded = set()
            available = set()
            for message in thread["messages"]:
                result = render_message(
                    message["body_text"], message["body_html"], message["attachments"]
                )
                embedded |= result.embedded_ids
                available |= {
                    item["id"]
                    for item in message["attachments"]
                    if item["content_digest"] and item["mime_type"].startswith("image/")
                }
            documents.append(
                {
                    "date": row["sent_at"],
                    "original_available": bool(row["content_digest"]),
                    "extracted_characters": len(row["extracted_text"]),
                    "embedded_images_resolved": len(embedded & available),
                    "available_image_files": len(available),
                }
            )
        report["expected_files"].append({"filename": filename, "sources": documents})
    # Inspect the user's other screenshot example without emitting its private
    # message text, raw bytes, identifiers, or decrypted filesystem paths.
    from PySide6.QtGui import QImage, QImageReader
    QImageReader.setAllocationLimit(64)
    report["auto_quote_images"] = []
    for row in context.database.query(
        "SELECT id FROM threads WHERE upper(trim(subject))='AUTO QUOTE' AND trashed_at IS NULL ORDER BY latest_at DESC LIMIT 3"
    ):
        thread = context.repository.get_thread(row["id"])
        for message in thread["messages"]:
            rendered = render_message(message["body_text"], message["body_html"], message["attachments"])
            images = []
            for item in message["attachments"]:
                if not item["mime_type"].startswith("image/"):
                    continue
                value = QImage()
                available = bool(item["content_digest"])
                if available:
                    value.loadFromData(context.repository.blobs.get(item["content_digest"]))
                images.append({"filename": item["filename"], "available": available,
                    "decoded": not value.isNull(), "width": value.width(), "height": value.height(),
                    "referenced_inline": item["id"] in rendered.embedded_ids})
            report["auto_quote_images"].append({"image_count": len(images), "images": images})
    report["rendered_mail"] = audit_rendered_mail(context.database)
    return report


def rendered_link_gaps(markup: str) -> dict[str, int]:
    """Count visibly raw link tokens, never return sender content or URLs."""
    soup = BeautifulSoup(markup, "html.parser")
    gaps = {"raw_cid_text": 0, "bare_web_text": 0, "bare_email_text": 0,
            "external_image_sources": 0, "clickable_links": 0}
    for image in soup.find_all("img", src=True):
        if str(image["src"]).lower().startswith(("http://", "https://")):
            gaps["external_image_sources"] += 1
    gaps["clickable_links"] = len(soup.find_all("a", href=True))
    for node in soup.find_all(string=True):
        if node.find_parent("a") is not None:
            continue
        value = str(node)
        gaps["raw_cid_text"] += len(re.findall(r"(?i)\bcid:[^\s<>]+", value))
        gaps["bare_web_text"] += len(re.findall(r"(?i)(?:https?://|www\.)[^\s<>]+", value))
        gaps["bare_email_text"] += len(re.findall(
            r"(?i)(?<![\w@])[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}", value
        ))
    return gaps


def audit_rendered_mail(database) -> dict:
    """Audit every stored, non-trashed message without emitting private text."""
    attachments: dict[str, list[dict]] = {}
    for row in database.query(
        "SELECT a.id,a.message_id,a.provider_id,a.filename,a.mime_type,a.content_id,a.content_location,a.content_digest "
        "FROM attachments a JOIN messages m ON m.id=a.message_id "
        "JOIN threads t ON t.id=m.thread_id WHERE t.trashed_at IS NULL"
    ):
        attachments.setdefault(row["message_id"], []).append(dict(row))
    report = {"messages": 0, "render_failures": 0, "embedded_images": 0,
              "remote_image_urls_blocked": 0, "raw_cid_text": 0,
              "bare_web_text": 0, "bare_email_text": 0,
              "external_image_sources": 0, "clickable_links": 0}
    for row in database.query(
        "SELECT m.id,m.body_text,m.body_html FROM messages m "
        "JOIN threads t ON t.id=m.thread_id WHERE t.trashed_at IS NULL"
    ):
        report["messages"] += 1
        try:
            rendered = render_message(
                row["body_text"], row["body_html"], attachments.get(row["id"], [])
            )
            report["embedded_images"] += len(rendered.embedded_ids)
            report["remote_image_urls_blocked"] += len(rendered.remote_urls)
            for key, count in rendered_link_gaps(rendered.html).items():
                report[key] += count
        except Exception:
            report["render_failures"] += 1
    return report


def rendering_gap_review_text(database) -> str:
    """Show only local, in-memory examples; never print private mail to logs."""
    attachments: dict[str, list[dict]] = {}
    for row in database.query(
        "SELECT a.id,a.message_id,a.provider_id,a.filename,a.mime_type,a.content_id,a.content_location,a.content_digest "
        "FROM attachments a JOIN messages m ON m.id=a.message_id "
        "JOIN threads t ON t.id=m.thread_id WHERE t.trashed_at IS NULL"
    ):
        attachments.setdefault(row["message_id"], []).append(dict(row))
    patterns = (
        ("web", re.compile(r"(?i)(?:https?://|www\.)[^\s<>]+")),
        ("email", re.compile(r"(?i)(?<![\w@])[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}")),
    )
    lines = ["Private rendered-link review (local only; no AI or provider action)"]
    found = 0
    for row in database.query(
        "SELECT m.id,m.subject,m.body_text,m.body_html FROM messages m "
        "JOIN threads t ON t.id=m.thread_id WHERE t.trashed_at IS NULL ORDER BY m.rowid"
    ):
        rendered = render_message(
            row["body_text"], row["body_html"], attachments.get(row["id"], [])
        )
        soup = BeautifulSoup(rendered.html, "html.parser")
        for node in soup.find_all(string=True):
            if node.find_parent("a") is not None:
                continue
            value = str(node)
            for kind, pattern in patterns:
                for match in pattern.finditer(value):
                    found += 1
                    token = match.group()
                    original = row["body_html"] or row["body_text"]
                    position = original.find(token)
                    excerpt = (
                        original[max(0, position - 100):position + len(token) + 100]
                        if position >= 0 else "Source token location unavailable"
                    )
                    lines.extend((
                        "",
                        f"CASE {found} — {kind} — {row['subject']}",
                        "Visible rendered context: " + value[max(0, match.start() - 100):match.end() + 100],
                        "Stored source context: " + excerpt,
                    ))
                    if found >= 12:
                        return "\n".join(lines)
    if not found:
        lines.append("No unlinked web or email tokens detected in the stored messages.")
    return "\n".join(lines)


def benchmark_work_path(recovery: Path, phase: str, label: str = "") -> Path:
    if phase not in {"before", "after", "luna", "shared-tone", "single-pass"}:
        raise ValueError("Choose a supported benchmark phase")
    if label and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", label):
        raise ValueError("Use a short lowercase benchmark label without path characters")
    return recovery / f"reply-benchmark-{phase}{'-' + label if label else ''}.db"


def actionable_reply_thread_ids(
    database: Database, account_ids: list[str], max_age_days: int | None = None,
    require_prior_sent: bool = False,
) -> list[str]:
    """Choose incoming-last conversations, optionally requiring recent two-way mail."""
    cutoff = (
        (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        if max_age_days is not None else "0001-01-01T00:00:00+00:00"
    )
    groups = []
    for account_id in account_ids:
        rows = database.query(
            """WITH ranked AS (
                 SELECT m.thread_id,m.is_sent,m.in_main_inbox,m.is_filtered_bulk,m.sent_at,
                        m.sender_email,m.body_text,m.internet_message_id,
                        length(m.body_text) AS body_length,
                        ROW_NUMBER() OVER (
                          PARTITION BY m.thread_id ORDER BY m.sent_at DESC,m.id DESC
                        ) AS position
                 FROM messages m JOIN threads t ON t.id=m.thread_id
                 WHERE t.account_id=? AND t.trashed_at IS NULL AND m.is_draft=0
               )
               SELECT t.id,t.subject,r.sender_email,r.body_text,r.sent_at,r.internet_message_id
               FROM threads t JOIN ranked r ON r.thread_id=t.id
               WHERE t.account_id=? AND r.position=1 AND r.is_sent=0
                 AND r.in_main_inbox=1 AND r.is_filtered_bulk=0
                 AND r.body_length BETWEEN 80 AND 20000
                 AND julianday(r.sent_at)>=julianday(?)
                 AND (?=0 OR EXISTS(
                   SELECT 1 FROM messages prior WHERE prior.thread_id=t.id
                     AND prior.is_sent=1 AND prior.is_draft=0
                     AND julianday(prior.sent_at)<julianday(r.sent_at)
                 ))
               ORDER BY t.message_count DESC,t.latest_at DESC,t.id LIMIT 200""",
            (account_id, account_id, cutoff, int(require_prior_sent)),
        )
        groups.append([
            row for row in rows
            if not reply_unavailable(row["sender_email"], row["body_text"])
            and not reply_requires_instruction(row["body_text"])
        ])
    identifiers = []
    seen_messages = set()
    while len(identifiers) < 10 and any(groups):
        for group in groups:
            if group and len(identifiers) < 10:
                row = group.pop(0)
                message_id = str(row["internet_message_id"] or "").strip().casefold()
                fingerprint = (
                    ("message_id", message_id) if message_id else
                    ("content", str(row["sender_email"]).casefold(), row["sent_at"],
                     str(row["subject"]).strip().casefold(),
                     " ".join(str(row["body_text"]).split()))
                )
                if fingerprint in seen_messages:
                    continue
                seen_messages.add(fingerprint)
                identifiers.append(row["id"])
    if len(identifiers) != 10:
        raise ValueError("Ten currently actionable historical threads are required")
    return identifiers


def observed_reply_pairs(database: Database, account_ids: list[str]) -> list[tuple[str, str, str]]:
    """Find real incoming-to-owner-reply pairs for a future matched quality test.

    Keep IDs in memory only. A sent message is eligible only when the immediately
    preceding completed message in that same thread was incoming.
    """
    if not account_ids:
        return []
    placeholders = ",".join("?" for _ in account_ids)
    rows = database.query(
        f"""WITH ordered AS (
              SELECT m.id,m.thread_id,m.account_id,m.is_sent,m.sent_at,m.body_text,
                     LAG(m.id) OVER (
                       PARTITION BY m.thread_id ORDER BY julianday(m.sent_at),m.id
                     ) AS previous_id
              FROM messages m JOIN threads t ON t.id=m.thread_id
              WHERE t.trashed_at IS NULL AND m.is_draft=0
                AND m.account_id IN ({placeholders})
            )
            SELECT s.thread_id,s.id AS sent_id,s.sent_at AS sent_at,r.id AS incoming_id,
                   r.internet_message_id,r.sender_email,r.sent_at AS incoming_at,
                   r.received_at AS incoming_received_at,
                   r.subject,r.body_text AS incoming_body,s.body_text AS sent_body
            FROM ordered s JOIN messages r ON r.id=s.previous_id
            WHERE s.is_sent=1 AND r.is_sent=0 AND r.is_filtered_bulk=0
              AND length(r.body_text) BETWEEN 80 AND 20000
            ORDER BY julianday(s.sent_at) DESC,s.id DESC""",
        tuple(account_ids),
    )
    pairs: list[tuple[str, str, str]] = []
    seen_threads: set[str] = set()
    seen_incoming: set[tuple] = set()
    for row in rows:
        if row["thread_id"] in seen_threads or reply_unavailable(
            row["sender_email"], row["incoming_body"]
        ):
            continue
        if len(clean_sent_body(row["sent_body"]).strip()) < 10:
            continue
        incoming_at = datetime.fromisoformat(str(row["incoming_at"]).replace("Z", "+00:00"))
        received_at = datetime.fromisoformat(str(row["incoming_received_at"]).replace("Z", "+00:00"))
        sent_at = datetime.fromisoformat(str(row["sent_at"]).replace("Z", "+00:00"))
        if incoming_at >= sent_at or received_at >= sent_at:
            continue
        message_id = str(row["internet_message_id"] or "").strip().casefold()
        fingerprint = (
            ("message_id", message_id) if message_id else
            ("content", str(row["sender_email"]).casefold(), row["incoming_at"],
             str(row["subject"]).strip().casefold(),
             " ".join(str(row["incoming_body"]).split()))
        )
        if fingerprint in seen_incoming:
            continue
        seen_threads.add(row["thread_id"])
        seen_incoming.add(fingerprint)
        pairs.append((row["thread_id"], row["incoming_id"], row["sent_id"]))
    return pairs


def prune_observed_reply_snapshot(database: Database, incoming_id: str, sent_id: str) -> datetime:
    """Make an encrypted diagnostic copy reflect the instant before a real reply.

    The caller must pass a copy, never the live database or fixed seed. Removing
    later mail also removes its search chunks, facts, examples and cached notes,
    so neither retrieval nor tone selection can reveal the held-out reply.
    """
    row = database.connection.execute(
        """SELECT i.thread_id,i.sent_at AS incoming_at,s.sent_at AS sent_at,
                  s.internet_message_id AS sent_message_id
           FROM messages i JOIN messages s ON s.thread_id=i.thread_id
           WHERE i.id=? AND s.id=? AND i.is_sent=0 AND s.is_sent=1""",
        (incoming_id, sent_id),
    ).fetchone()
    if not row:
        raise ValueError("The observed reply pair is unavailable")
    incoming_at = datetime.fromisoformat(str(row["incoming_at"]).replace("Z", "+00:00"))
    sent_at = datetime.fromisoformat(str(row["sent_at"]).replace("Z", "+00:00"))
    if incoming_at >= sent_at:
        raise ValueError("The observed reply must follow the incoming message")
    message_id = str(row["sent_message_id"] or "").strip().casefold()
    sent_row = database.connection.execute(
        "SELECT sender_email,subject,body_text FROM messages WHERE id=?", (sent_id,)
    ).fetchone()
    doomed = (
        "(julianday(m.sent_at)>=julianday(?) "
        "OR julianday(m.received_at)>=julianday(?)) OR m.id=? "
        "OR (?<>'' AND lower(m.internet_message_id)=?) "
        "OR (m.is_sent=1 AND m.sender_email=? AND m.subject=? AND m.body_text=?)"
    )
    parameters = (
        row["sent_at"], row["sent_at"], sent_id, message_id, message_id,
        sent_row["sender_email"], sent_row["subject"], sent_row["body_text"],
    )
    with database.transaction() as connection:
        connection.execute("DELETE FROM summaries")
        connection.execute("DELETE FROM tone_notes")
        connection.execute("DELETE FROM draft_records")
        connection.execute(
            f"""DELETE FROM source_chunks WHERE
                (source_type='message' AND source_id IN
                    (SELECT m.id FROM messages m WHERE {doomed}))
                OR (source_type='attachment' AND source_id IN
                    (SELECT a.id FROM attachments a JOIN messages m ON m.id=a.message_id
                     WHERE {doomed}))""",
            (*parameters, *parameters),
        )
        connection.execute(
            f"DELETE FROM extracted_facts WHERE message_id IN "
            f"(SELECT m.id FROM messages m WHERE {doomed})",
            parameters,
        )
        connection.execute(
            f"DELETE FROM tone_examples WHERE message_id IN "
            f"(SELECT m.id FROM messages m WHERE {doomed})",
            parameters,
        )
        connection.execute(
            f"DELETE FROM attachments WHERE message_id IN "
            f"(SELECT m.id FROM messages m WHERE {doomed})",
            parameters,
        )
        connection.execute(f"DELETE FROM messages AS m WHERE {doomed}", parameters)
        connection.execute(
            """DELETE FROM source_chunks WHERE
               (source_type='message' AND NOT EXISTS
                   (SELECT 1 FROM messages m WHERE m.id=source_chunks.source_id))
               OR (source_type='attachment' AND NOT EXISTS
                   (SELECT 1 FROM attachments a WHERE a.id=source_chunks.source_id))"""
        )
        connection.execute(
            "DELETE FROM threads WHERE NOT EXISTS "
            "(SELECT 1 FROM messages m WHERE m.thread_id=threads.id)"
        )
        connection.execute(
            """UPDATE threads SET
                 subject=(SELECT m.subject FROM messages m WHERE m.thread_id=threads.id
                          ORDER BY m.sent_at DESC,m.id DESC LIMIT 1),
                 latest_at=(SELECT MAX(m.sent_at) FROM messages m WHERE m.thread_id=threads.id),
                 message_count=(SELECT COUNT(*) FROM messages m WHERE m.thread_id=threads.id),
                 has_attachments=EXISTS(SELECT 1 FROM attachments a JOIN messages m
                     ON m.id=a.message_id WHERE m.thread_id=threads.id),
                 is_unread=EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=threads.id
                     AND COALESCE(m.local_unread,m.is_unread)=1),
                 participants_json='[]',content_hash=''"""
        )
    if not database.scalar("SELECT 1 FROM messages WHERE id=?", (incoming_id,)):
        raise ValueError("The snapshot lost its incoming message")
    if database.scalar("SELECT 1 FROM messages WHERE id=?", (sent_id,)):
        raise ValueError("The held-out reply remains in the snapshot")
    if not database.integrity_check():
        raise ValueError("The historical snapshot failed integrity checks")
    return sent_at


def audit_observed_reply_snapshot(context, case_number: int) -> dict[str, bool | int]:
    """Prove a fixed-seed case can be pruned without printing private content."""
    seed = Database(
        context.paths.recovery / "reply-benchmark-seed.db",
        context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    seed.open()
    try:
        pairs = observed_reply_pairs(
            seed, [account["id"] for account in context.repository.list_accounts()]
        )
        if not 1 <= case_number <= len(pairs):
            raise ValueError("The observed reply case is unavailable")
        thread_id, incoming_id, sent_id = pairs[case_number - 1]
        with tempfile.TemporaryDirectory(
            prefix="reply-snapshot-audit-", dir=context.paths.recovery
        ) as temporary:
            path = Path(temporary) / "snapshot.db"
            seed.backup_to(path)
            snapshot = Database(
                path, context.database.passphrase,
                allow_plaintext=context.database.allow_plaintext,
            )
            snapshot.open()
            try:
                sent_at = prune_observed_reply_snapshot(snapshot, incoming_id, sent_id)
                latest = snapshot.scalar(
                    """SELECT id FROM messages WHERE thread_id=? AND is_draft=0
                       ORDER BY julianday(sent_at) DESC,id DESC LIMIT 1""",
                    (thread_id,),
                )
                no_future = snapshot.scalar(
                    """SELECT COUNT(*) FROM messages WHERE
                       julianday(sent_at)>=julianday(?) OR
                       julianday(received_at)>=julianday(?)""",
                    (sent_at.isoformat(), sent_at.isoformat()),
                ) == 0
                no_orphans = snapshot.scalar(
                    """SELECT COUNT(*) FROM source_chunks c WHERE
                       (c.source_type='message' AND NOT EXISTS
                           (SELECT 1 FROM messages m WHERE m.id=c.source_id))
                       OR (c.source_type='attachment' AND NOT EXISTS
                           (SELECT 1 FROM attachments a WHERE a.id=c.source_id))"""
                ) == 0
                result = {
                    "case": case_number,
                    "latest_is_incoming": latest == incoming_id,
                    "held_out_absent": snapshot.scalar(
                        "SELECT 1 FROM messages WHERE id=?", (sent_id,)
                    ) is None,
                    "no_future_messages": no_future,
                    "no_orphan_search_chunks": no_orphans,
                    "no_cached_tone": snapshot.scalar("SELECT COUNT(*) FROM tone_notes") == 0,
                    "integrity": snapshot.integrity_check(),
                }
                return result
            finally:
                snapshot.close()
    finally:
        seed.close()


def reply_cohort_availability(context) -> dict[str, bool | int]:
    """Report only whether the fixed encrypted seed has ten eligible threads."""
    seed_path = context.paths.recovery / "reply-benchmark-seed.db"
    if not seed_path.is_file():
        raise FileNotFoundError("The fixed reply benchmark seed is unavailable")
    seed = Database(
        seed_path,
        context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    seed.open()
    try:
        account_ids = [account["id"] for account in context.repository.list_accounts()]
        available: dict[str, bool | int] = {}
        for label, days, prior_sent in (
            ("recent_incoming", 30, False),
            ("recent_dialogue", 30, True),
            ("historical_dialogue", None, True),
        ):
            try:
                actionable_reply_thread_ids(seed, account_ids, days, prior_sent)
                available[label] = True
            except ValueError:
                available[label] = False
        observed_count = len(observed_reply_pairs(seed, account_ids))
        available["observed_user_replies"] = observed_count >= 10
        available["observed_user_reply_count"] = observed_count
        return available
    finally:
        seed.close()


def reply_result_shape(result: dict) -> dict:
    """Content-free checks; these do not establish factual accuracy or good style."""
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    bodies = [str(item.get("body", "")).strip() for item in candidates if isinstance(item, dict)]
    labels = [str(item.get("label", "")).strip() for item in candidates if isinstance(item, dict)]
    stances = [str(item.get("stance", "")).strip() for item in candidates if isinstance(item, dict)]
    decision = bool(result.get("decision_detected"))
    normalized = [re.sub(r"\s+", " ", body).casefold() for body in bodies]
    similarities = [
        SequenceMatcher(None, normalized[left], normalized[right]).ratio()
        for left in range(len(normalized)) for right in range(left + 1, len(normalized))
    ]
    return {
        "three_candidates": len(candidates) == 3 and len(bodies) == 3,
        "nonempty_bodies": len(bodies) == 3 and all(bodies),
        "distinct_bodies": len(set(normalized)) == 3,
        "distinct_labels": len(labels) == 3 and all(labels) and len(set(labels)) == 3,
        "variant_labels": decision or {label.casefold() for label in labels} == {
            "concise", "warm", "detailed"
        },
        "decision_stances": not decision or (
            len(stances) == 3 and all(stances) and len(set(stance.casefold() for stance in stances)) == 3
        ),
        "source_markers_in_body": any("[SOURCE:" in body or "[ATTACHMENT:" in body for body in bodies),
        "max_body_similarity": round(max(similarities, default=0.0), 3),
        "median_body_chars": round(statistics.median(map(len, bodies)), 1) if bodies else 0,
        "decision_detected": decision,
    }


def reply_references_resolve(database: Database, result: dict, account_id: str) -> bool:
    for candidate in result.get("candidates", []):
        for identifier in candidate.get("supporting_source_ids", []):
            if not database.scalar(
                "SELECT 1 FROM messages WHERE id=? AND account_id=? "
                "UNION SELECT 1 FROM attachments a JOIN messages m ON m.id=a.message_id "
                "WHERE a.id=? AND m.account_id=? "
                "UNION SELECT 1 FROM source_chunks WHERE id=? AND account_id=?",
                (identifier, account_id, identifier, account_id, identifier, account_id),
            ):
                return False
        for identifier in candidate.get("tone_example_ids", []):
            if not database.scalar(
                "SELECT 1 FROM messages WHERE id=? AND account_id=? AND is_sent=1",
                (identifier, account_id),
            ):
                return False
    return True


def review_reply_benchmark_pair(context, first_label: str, second_label: str) -> dict:
    """Compare private benchmark results while releasing only aggregate flags."""
    databases = []
    try:
        records = []
        for label in (first_label, second_label):
            path = benchmark_work_path(context.paths.recovery, "after", label)
            if not path.is_file():
                raise FileNotFoundError("A requested encrypted reply benchmark is missing")
            database = Database(
                path, context.database.passphrase,
                allow_plaintext=context.database.allow_plaintext,
            )
            database.open()
            databases.append(database)
            raw = database.scalar(
                "SELECT value FROM app_settings WHERE key='benchmark_reply_results'"
            )
            if not raw:
                raise ValueError("A requested reply benchmark has no completed results")
            records.append(json.loads(raw)["runs"])
        if len(records[0]) != 10 or len(records[1]) != 10 or [
            row["thread_id"] for row in records[0]
        ] != [row["thread_id"] for row in records[1]]:
            raise ValueError("Reply benchmarks must contain the same ten threads in the same order")
        if [row.get("observed_case") for row in records[0]] != [
            row.get("observed_case") for row in records[1]
        ]:
            raise ValueError("Reply benchmarks must use the same historical cases")
        cases = []
        for index, pair in enumerate(zip(*records, strict=True), 1):
            account_id = databases[0].scalar(
                "SELECT account_id FROM threads WHERE id=?", (pair[0]["thread_id"],)
            )
            if not account_id:
                raise ValueError("A benchmark source conversation is missing")
            first_shape = reply_result_shape(pair[0]["result"])
            second_shape = reply_result_shape(pair[1]["result"])
            first_shape["references_resolve"] = reply_references_resolve(
                databases[0], pair[0]["result"], account_id
            )
            second_shape["references_resolve"] = reply_references_resolve(
                databases[1], pair[1]["result"], account_id
            )
            if "snapshot_references_resolve" in pair[0]:
                first_shape["snapshot_references_resolve"] = pair[0]["snapshot_references_resolve"]
            if "snapshot_references_resolve" in pair[1]:
                second_shape["snapshot_references_resolve"] = pair[1]["snapshot_references_resolve"]
            cases.append({
                "case": index,
                first_label: first_shape,
                second_label: second_shape,
                "same_private_intent": bool(pair[0].get("intent_supplied"))
                and bool(pair[1].get("intent_supplied"))
                and pair[0].get("intent_digest") == pair[1].get("intent_digest"),
                "private_intent_supplied": bool(pair[0].get("intent_supplied"))
                and bool(pair[1].get("intent_supplied")),
                "decision_agreement": bool(pair[0]["result"].get("decision_detected"))
                == bool(pair[1]["result"].get("decision_detected")),
            })
        return {
            "same_ten_threads": True,
            "first_label": first_label,
            "second_label": second_label,
            "cases": cases,
            "scope": "structural checks only; factual accuracy and tone still need review",
        }
    finally:
        for database in databases:
            database.close()


def format_reply_review_case(
    thread: dict, first_result: dict, second_result: dict,
    first_label: str, second_label: str, case_number: int,
) -> str:
    """Keep private review text in memory for a local, read-only window."""
    lines = format_reply_review_thread(thread, case_number)
    for label, result in ((first_label, first_result), (second_label, second_result)):
        lines.extend([
            "",
            "=" * 60,
            f"{label.upper()} — decision_detected={result['decision_detected']}",
            f"Decision question: {result.get('decision_question', '')}",
        ])
        for choice in result["candidates"]:
            lines.extend([
                "",
                f"{choice['label']} | stance: {choice['stance']} | assumption: {choice['assumption']}",
                choice["body"],
            ])
    return "\n".join(lines)


def format_reply_review_thread(thread: dict, case_number: int) -> list[str]:
    lines = [
        f"CASE {case_number} — {thread['subject']}",
        f"Account: {thread['account_email']}",
        "",
        "RECENT CONVERSATION (new content, oldest first)",
    ]
    for message in [item for item in thread["messages"] if not item.get("is_draft")][-4:]:
        lines.extend([
            "",
            f"From: {message['sender_name']} <{message['sender_email']}>",
            f"Date: {message['sent_at']}",
            (message.get("new_content") or message["body_text"])[:5000],
        ])
    return lines


def reply_cohort_case_text(context, cohort: str, case_number: int) -> str:
    """Show source context privately, including a case whose AI run failed."""
    if cohort not in {"actionable", "recent", "recent-dialogue", "observed"} or not 1 <= case_number <= (50 if cohort == "observed" else 10):
        raise ValueError("Choose a supported cohort and case")
    seed = Database(
        context.paths.recovery / "reply-benchmark-seed.db",
        context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    seed.open()
    try:
        if cohort == "observed":
            pairs = observed_reply_pairs(
                seed, [account["id"] for account in context.repository.list_accounts()]
            )
            if len(pairs) < case_number:
                raise ValueError("This observed reply case is unavailable")
            thread_id, incoming_id, sent_id = pairs[case_number - 1]
            thread = MailRepository(seed, context.repository.blobs).get_thread(thread_id)
            if not thread:
                raise ValueError("The benchmark conversation is unavailable")
            ordered = sorted(
                [message for message in thread["messages"] if not message.get("is_draft")],
                key=lambda item: (item["sent_at"], item["id"]),
            )
            sent_index = next(
                (index for index, message in enumerate(ordered) if message["id"] == sent_id),
                None,
            )
            if sent_index is None or sent_index == 0 or ordered[sent_index - 1]["id"] != incoming_id:
                raise ValueError("The observed reply pair is inconsistent")
            lines = format_reply_review_thread(
                {**thread, "messages": ordered[:sent_index]}, case_number
            )
            actual = ordered[sent_index]
            lines.extend([
                "", "ACTUAL USER-APPROVED REPLY (held out from candidate context)",
                (actual.get("new_content") or actual["body_text"])[:5000],
            ])
            return "\n".join(lines)
        identifiers = actionable_reply_thread_ids(
            seed, [account["id"] for account in context.repository.list_accounts()],
            max_age_days=30 if cohort in {"recent", "recent-dialogue"} else None,
            require_prior_sent=cohort == "recent-dialogue",
        )
        thread = MailRepository(seed, context.repository.blobs).get_thread(
            identifiers[case_number - 1]
        )
        if not thread:
            raise ValueError("The benchmark conversation is unavailable")
        lines = format_reply_review_thread(thread, case_number)
        latest = max(
            (message for message in thread["messages"] if not message.get("is_draft")),
            key=lambda item: (item["sent_at"], item["id"]),
        )
        lines.insert(2, f"Latest completed message: {'sent' if latest['is_sent'] else 'incoming'}")
        return "\n".join(lines)
    finally:
        seed.close()


def reply_case_review_text(
    context, first_label: str, second_label: str, case_number: int
) -> str:
    if not 1 <= case_number <= 10:
        raise ValueError("Choose a case from the ten-thread benchmark")
    databases = []
    try:
        runs = []
        for label in (first_label, second_label):
            path = benchmark_work_path(context.paths.recovery, "after", label)
            if not path.is_file():
                raise FileNotFoundError("A requested encrypted reply benchmark is missing")
            database = Database(
                path, context.database.passphrase,
                allow_plaintext=context.database.allow_plaintext,
            )
            database.open()
            databases.append(database)
            raw = database.scalar(
                "SELECT value FROM app_settings WHERE key='benchmark_reply_results'"
            )
            if not raw:
                raise ValueError("A requested reply benchmark has no completed results")
            runs.append(json.loads(raw)["runs"])
        if len(runs[0]) != 10 or len(runs[1]) != 10:
            raise ValueError("Both reply benchmarks must contain ten results")
        first, second = runs[0][case_number - 1], runs[1][case_number - 1]
        if first["thread_id"] != second["thread_id"]:
            raise ValueError("Reply benchmarks do not refer to the same conversation")
        thread = MailRepository(databases[0], context.repository.blobs).get_thread(
            first["thread_id"]
        )
        if not thread:
            raise ValueError("The benchmark conversation is unavailable")
        actual_reply = ""
        if "observed_case" in first:
            pairs = observed_reply_pairs(
                databases[0],
                [account["id"] for account in context.repository.list_accounts()],
            )
            observed_case = int(first["observed_case"])
            if not 1 <= observed_case <= len(pairs):
                raise ValueError("The observed comparison source is unavailable")
            thread_id, incoming_id, sent_id = pairs[observed_case - 1]
            if thread_id != first["thread_id"] or observed_case != second.get("observed_case"):
                raise ValueError("The observed comparison source changed")
            ordered = sorted(
                [item for item in thread["messages"] if not item.get("is_draft")],
                key=lambda item: (item["sent_at"], item["id"]),
            )
            sent_index = next(
                (index for index, item in enumerate(ordered) if item["id"] == sent_id), None
            )
            if sent_index is None or sent_index == 0 or ordered[sent_index - 1]["id"] != incoming_id:
                raise ValueError("The observed comparison source is inconsistent")
            actual_reply = (ordered[sent_index].get("new_content") or
                            ordered[sent_index]["body_text"])[:5000]
            thread = {**thread, "messages": ordered[:sent_index]}
        review = format_reply_review_case(
            thread, first["result"], second["result"],
            first_label, second_label, case_number,
        )
        if actual_reply:
            review += "\n\nACTUAL USER-APPROVED REPLY (held out)\n" + actual_reply
        return review
    finally:
        for database in databases:
            database.close()


OBSERVED_BENCHMARK_CASES = (2, 4, 5, 6, 7, 8, 9, 12, 14, 16)


def _observed_intent_path(recovery: Path) -> Path:
    return recovery / "reply-benchmark-intents.db"


def _observed_intents(context, incoming_ids: list[str]) -> dict[str, str]:
    """Read private diagnostic instructions from a separate encrypted store."""
    path = _observed_intent_path(context.paths.recovery)
    if not path.is_file() or not incoming_ids:
        return {}
    database = Database(
        path, context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    database.open()
    try:
        return {
            incoming_id: str(database.scalar(
                "SELECT value FROM app_settings WHERE key=?",
                (f"observed-intent:{incoming_id}",),
            ) or "")
            for incoming_id in incoming_ids
        }
    finally:
        database.close()


def observed_reply_intent(context, case_number: int) -> str:
    """Load one instruction without putting private text in a command argument."""
    seed = Database(
        context.paths.recovery / "reply-benchmark-seed.db",
        context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    seed.open()
    try:
        pairs = observed_reply_pairs(
            seed, [account["id"] for account in context.repository.list_accounts()]
        )
        if not 1 <= case_number <= len(pairs):
            raise ValueError("The observed reply case is unavailable")
        incoming_id = pairs[case_number - 1][1]
    finally:
        seed.close()
    return _observed_intents(context, [incoming_id]).get(incoming_id, "")


def save_observed_reply_intent(context, case_number: int, instruction: str) -> None:
    """Save a human-supplied intent in encrypted diagnostics, never in the seed."""
    instruction = instruction.strip()
    if not 1 <= len(instruction) <= 1200:
        raise ValueError("Write a short instruction before saving")
    seed = Database(
        context.paths.recovery / "reply-benchmark-seed.db",
        context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    seed.open()
    try:
        pairs = observed_reply_pairs(
            seed, [account["id"] for account in context.repository.list_accounts()]
        )
        if not 1 <= case_number <= len(pairs):
            raise ValueError("The observed reply case is unavailable")
        incoming_id = pairs[case_number - 1][1]
    finally:
        seed.close()
    database = Database(
        _observed_intent_path(context.paths.recovery), context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    database.open()
    try:
        database.execute(
            """INSERT INTO app_settings(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (f"observed-intent:{incoming_id}", instruction),
        )
    finally:
        database.close()


def benchmark_observed_replies(
    context, phase: str, label: str, effort: str, compact: bool,
    require_intents: bool = False,
) -> None:
    """Time ten drafts against progressively older, answer-free snapshots."""
    recovery = context.paths.recovery
    destination = benchmark_work_path(recovery, phase, label)
    if destination.exists():
        raise ValueError("This benchmark phase already exists; keep its measurements")
    seed = Database(
        recovery / "reply-benchmark-seed.db", context.database.passphrase,
        allow_plaintext=context.database.allow_plaintext,
    )
    seed.open()
    try:
        pairs = observed_reply_pairs(
            seed, [account["id"] for account in context.repository.list_accounts()]
        )
        if len(pairs) < max(OBSERVED_BENCHMARK_CASES):
            raise ValueError("The fixed seed has too few observed replies")
        selected = [pairs[number - 1] for number in OBSERVED_BENCHMARK_CASES]
        intents = _observed_intents(context, [incoming_id for _, incoming_id, _ in selected])
        if require_intents and any(
            not intents.get(incoming_id, "").strip() for _, incoming_id, _ in selected
        ):
            raise ValueError("Save a private intent for all ten selected cases first")
        with tempfile.TemporaryDirectory(prefix="reply-benchmark-observed-", dir=recovery) as temporary:
            snapshot_path = Path(temporary) / "snapshot.db"
            result_path = Path(temporary) / "results.db"
            seed.backup_to(snapshot_path)
            seed.backup_to(result_path)
            snapshot = Database(
                snapshot_path, context.database.passphrase,
                allow_plaintext=context.database.allow_plaintext,
            )
            results = Database(
                result_path, context.database.passphrase,
                allow_plaintext=context.database.allow_plaintext,
            )
            snapshot.open()
            results.open()
            try:
                settings = replace(context.settings)
                settings.reasoning_effort = effort
                settings.compact_reply_experiment = compact
                if phase == "luna":
                    settings.response_model = "gpt-5.6-luna"
                runs = []
                previous_time: datetime | None = None
                for index, (thread_id, incoming_id, sent_id) in enumerate(selected, 1):
                    reference_now = prune_observed_reply_snapshot(
                        snapshot, incoming_id, sent_id
                    )
                    if previous_time is not None and reference_now >= previous_time:
                        raise ValueError("Observed cases must be ordered newest to oldest")
                    previous_time = reference_now
                    latest_id = snapshot.scalar(
                        """SELECT id FROM messages WHERE thread_id=? AND is_draft=0
                           ORDER BY julianday(sent_at) DESC,id DESC LIMIT 1""",
                        (thread_id,),
                    )
                    if latest_id != incoming_id:
                        raise ValueError("The snapshot has a newer message than the target")
                    repository = MailRepository(snapshot, context.repository.blobs)
                    ai = OpenAIService(
                        context.vault, settings, context.openai.usage,
                        context.openai.codex_executable,
                    )
                    assistant = AssistantService(
                        repository, HybridSearch(snapshot, repository, ai), ai, settings
                    )
                    started = time.perf_counter()
                    instruction = intents.get(incoming_id, "")
                    result = assistant.draft_reply(
                        thread_id, instruction, reference_now=reference_now
                    )
                    elapsed = time.perf_counter() - started
                    account_id = snapshot.scalar(
                        "SELECT account_id FROM threads WHERE id=?", (thread_id,)
                    )
                    runs.append({
                        "thread_id": thread_id,
                        "observed_case": OBSERVED_BENCHMARK_CASES[index - 1],
                        "seconds": elapsed,
                        "stage_seconds": dict(assistant.last_reply_timings),
                        "intent_supplied": bool(instruction.strip()),
                        "intent_digest": hashlib.sha256(instruction.encode()).hexdigest(),
                        "snapshot_references_resolve": reply_references_resolve(
                            snapshot, result.model_dump(mode="json"), account_id
                        ),
                        "result": result.model_dump(mode="json"),
                    })
                    results.execute(
                        """INSERT INTO app_settings(key,value)
                           VALUES('benchmark_reply_results',?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        (json.dumps({
                            "phase": phase, "label": label,
                            "model": settings.response_model, "effort": effort,
                            "compact": compact, "cohort": "observed",
                            "required_intents": require_intents,
                            "cases": OBSERVED_BENCHMARK_CASES, "runs": runs,
                        }),),
                    )
                    print(
                        f"Reply {index}/10: {elapsed:.2f}s; candidates={len(result.candidates)}",
                        flush=True,
                    )
                print(
                    f"Median time: {statistics.median(item['seconds'] for item in runs):.2f}s",
                    flush=True,
                )
            finally:
                results.close()
                snapshot.close()
                # Preserve encrypted partial timings if a later AI request fails.
                # A new label is required to retry, so earlier evidence is not
                # silently overwritten or lost with the temporary snapshot.
                result_path.replace(destination)
    finally:
        seed.close()


def benchmark_replies(
    context, phase: str, label: str = "", effort: str = "low", compact: bool = False,
    cohort: str = "legacy", require_observed_intents: bool = False,
) -> None:
    if effort not in {"none", "low"}:
        raise ValueError("Choose a supported benchmark reasoning effort")
    if cohort == "observed":
        benchmark_observed_replies(
            context, phase, label, effort, compact, require_observed_intents
        )
        return
    if require_observed_intents:
        raise ValueError("Private intents apply only to the observed cohort")
    if cohort not in {"legacy", "actionable", "recent", "recent-dialogue"}:
        raise ValueError("Choose a supported benchmark cohort")
    seed_path = context.paths.recovery / "reply-benchmark-seed.db"
    work_path = benchmark_work_path(context.paths.recovery, phase, label)
    if work_path.exists():
        raise ValueError("This benchmark phase already exists; keep its measurements")
    if phase == "before":
        if seed_path.exists():
            raise ValueError("A benchmark seed already exists")
        context.database.backup_to(seed_path)
    if not seed_path.exists():
        raise ValueError("Record the before benchmark first")
    seed = Database(
        seed_path, context.database.passphrase, allow_plaintext=context.database.allow_plaintext
    )
    seed.open()
    try:
        selected = seed.scalar("SELECT value FROM app_settings WHERE key='benchmark_thread_ids'") if cohort == "legacy" else None
        if cohort in {"actionable", "recent", "recent-dialogue"}:
            selected = json.dumps(actionable_reply_thread_ids(
                seed, [account["id"] for account in context.repository.list_accounts()],
                max_age_days=30 if cohort in {"recent", "recent-dialogue"} else None,
                require_prior_sent=cohort == "recent-dialogue",
            ))
        elif not selected:
            groups = []
            for account in context.repository.list_accounts():
                rows = seed.query(
                    """SELECT t.id FROM threads t WHERE t.account_id=? AND t.trashed_at IS NULL
                       AND EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=t.id AND m.is_sent=0
                           AND m.is_draft=0 AND m.in_main_inbox=1 AND m.is_filtered_bulk=0
                           AND length(m.body_text) BETWEEN 80 AND 20000)
                       ORDER BY t.message_count DESC,t.latest_at DESC LIMIT 10""",
                    (account["id"],),
                )
                groups.append([row["id"] for row in rows])
            identifiers = []
            while len(identifiers) < 10 and any(groups):
                for group in groups:
                    if group and len(identifiers) < 10:
                        identifiers.append(group.pop(0))
            if len(identifiers) != 10:
                raise ValueError("Ten eligible historical threads are required")
            selected = json.dumps(identifiers)
            seed.execute(
                "INSERT INTO app_settings(key,value) VALUES('benchmark_thread_ids',?)", (selected,)
            )
        seed.backup_to(work_path)
    finally:
        seed.close()
    database = Database(
        work_path, context.database.passphrase, allow_plaintext=context.database.allow_plaintext
    )
    database.open()
    try:
        repository = MailRepository(database, context.repository.blobs)
        # Use the real usage ledger so measurements cannot evade a spending cap.
        settings = replace(context.settings)
        settings.reasoning_effort = effort
        settings.compact_reply_experiment = compact
        if phase == "luna":
            settings.response_model = "gpt-5.6-luna"
        ai = OpenAIService(context.vault, settings, context.openai.usage, context.openai.codex_executable)
        search = HybridSearch(database, repository, ai)
        assistant = AssistantService(repository, search, ai, settings)
        runs = []
        for index, identifier in enumerate(json.loads(selected), 1):
            started = time.perf_counter()
            result = assistant.draft_reply(identifier)
            elapsed = time.perf_counter() - started
            runs.append(
                {
                    "thread_id": identifier,
                    "seconds": elapsed,
                    "stage_seconds": dict(assistant.last_reply_timings),
                    "result": result.model_dump(mode="json"),
                }
            )
            database.execute(
                "INSERT INTO app_settings(key,value) VALUES('benchmark_reply_results',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    json.dumps(
                        {"phase": phase, "label": label, "model": settings.response_model,
                         "effort": effort, "compact": compact, "cohort": cohort, "runs": runs}
                    ),
                ),
            )
            print(
                f"Reply {index}/10: {elapsed:.2f}s; candidates={len(result.candidates)}", flush=True
            )
        print(
            f"Median time: {statistics.median(item['seconds'] for item in runs):.2f}s", flush=True
        )
    finally:
        database.close()
