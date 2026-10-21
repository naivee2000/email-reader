from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK(provider IN ('gmail','outlook')),
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#1A73E8',
    enabled INTEGER NOT NULL DEFAULT 1,
    sync_from TEXT,
    sync_cursor TEXT,
    sync_page_token TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    provider_thread_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    latest_at TEXT NOT NULL,
    participants_json TEXT NOT NULL DEFAULT '[]',
    message_count INTEGER NOT NULL DEFAULT 0,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    is_unread INTEGER NOT NULL DEFAULT 0,
    important_override INTEGER,
    source_state TEXT NOT NULL DEFAULT 'available',
    trashed_at TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    UNIQUE(account_id, provider_thread_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    provider_id TEXT NOT NULL,
    provider_revision TEXT,
    internet_message_id TEXT,
    in_reply_to TEXT,
    subject TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    recipients_json TEXT NOT NULL DEFAULT '{}',
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    labels_json TEXT NOT NULL DEFAULT '[]',
    is_sent INTEGER NOT NULL DEFAULT 0,
    is_draft INTEGER NOT NULL DEFAULT 0,
    is_unread INTEGER NOT NULL DEFAULT 0,
    in_main_inbox INTEGER NOT NULL DEFAULT 1,
    is_filtered_bulk INTEGER NOT NULL DEFAULT 0,
    mail_category TEXT NOT NULL DEFAULT 'primary' CHECK(mail_category IN ('primary','updates','')),
    source_state TEXT NOT NULL DEFAULT 'available',
    raw_digest TEXT,
    web_link TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, provider_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id),
    provider_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    content_digest TEXT,
    extracted_text TEXT NOT NULL DEFAULT '',
    extraction_metadata_json TEXT NOT NULL DEFAULT '{}',
    is_inline INTEGER NOT NULL DEFAULT 0,
    content_id TEXT NOT NULL DEFAULT '',
    content_location TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(message_id, provider_id)
);

CREATE TABLE IF NOT EXISTS source_chunks (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('message','attachment')),
    source_id TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    thread_id TEXT NOT NULL REFERENCES threads(id),
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    locator TEXT NOT NULL DEFAULT '',
    embedding_json TEXT,
    embedding_model TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_type, source_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS extracted_facts (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id),
    fact_type TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL DEFAULT '',
    source_excerpt TEXT NOT NULL,
    confidence REAL NOT NULL,
    important INTEGER NOT NULL,
    override_important INTEGER,
    override_value TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    content_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tone_examples (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    recipient_domain TEXT NOT NULL DEFAULT '',
    clean_body TEXT NOT NULL,
    approved_sent_version INTEGER NOT NULL DEFAULT 0,
    excluded_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_records (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    thread_id TEXT REFERENCES threads(id),
    provider_draft_id TEXT,
    selected_candidate_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    matched_sent_message_id TEXT REFERENCES messages(id)
);

CREATE TABLE IF NOT EXISTS tone_notes (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    recipient TEXT NOT NULL,
    document TEXT NOT NULL,
    example_ids_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, recipient)
);

CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    safe_message TEXT
);

CREATE TABLE IF NOT EXISTS main_inbox_scan_items (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    PRIMARY KEY(account_id, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_threads_latest ON threads(latest_at DESC);
CREATE TABLE IF NOT EXISTS source_retry_queue (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    provider_id TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    PRIMARY KEY(account_id,provider_id)
);
CREATE TABLE IF NOT EXISTS import_checkpoints (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    job_kind TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    cursor TEXT,
    page_token TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    since TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id,job_kind,policy_version)
);
CREATE TABLE IF NOT EXISTS import_exclusions (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    provider_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(account_id,provider_id)
);
CREATE TABLE IF NOT EXISTS enrichment_jobs (
    attachment_id TEXT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
    content_digest TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_enrichment_jobs (
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    body_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    email TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL,
    PRIMARY KEY(account_id,email)
);
CREATE INDEX IF NOT EXISTS idx_contacts_recent ON contacts(account_id,last_seen DESC);
CREATE TABLE IF NOT EXISTS contact_sources (
    account_id TEXT NOT NULL,
    email TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    PRIMARY KEY(account_id,email,message_id),
    FOREIGN KEY(account_id,email) REFERENCES contacts(account_id,email) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contact_sources_message ON contact_sources(message_id);
CREATE TABLE IF NOT EXISTS tone_recipients (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    PRIMARY KEY(message_id,email)
);
CREATE INDEX IF NOT EXISTS idx_tone_recipient_email ON tone_recipients(email,message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS message_header_fts USING fts5(
    message_id UNINDEXED, subject, sender_name, sender_email, recipients_json,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS headers_ai AFTER INSERT ON messages BEGIN
    INSERT INTO message_header_fts(rowid,message_id,subject,sender_name,sender_email,recipients_json)
    VALUES(new.rowid,new.id,new.subject,new.sender_name,new.sender_email,new.recipients_json);
END;
CREATE TRIGGER IF NOT EXISTS headers_ad AFTER DELETE ON messages BEGIN
    DELETE FROM message_header_fts WHERE rowid=old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS headers_au AFTER UPDATE OF subject,sender_name,sender_email,recipients_json ON messages
WHEN old.subject<>new.subject OR old.sender_name<>new.sender_name OR old.sender_email<>new.sender_email
 OR old.recipients_json<>new.recipients_json BEGIN
    DELETE FROM message_header_fts WHERE rowid=old.rowid;
    INSERT INTO message_header_fts(rowid,message_id,subject,sender_name,sender_email,recipients_json)
    VALUES(new.rowid,new.id,new.subject,new.sender_name,new.sender_email,new.recipients_json);
END;
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_provider ON messages(account_id, provider_id);
CREATE INDEX IF NOT EXISTS idx_draft_match ON draft_records(account_id,thread_id,matched_sent_message_id,created_at);
CREATE INDEX IF NOT EXISTS idx_draft_sent_lookup ON draft_records(account_id,matched_sent_message_id,created_at);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON source_chunks(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_facts_message ON extracted_facts(message_id);
CREATE INDEX IF NOT EXISTS idx_attachment_digest ON attachments(content_digest,mime_type);
CREATE INDEX IF NOT EXISTS idx_usage_month ON usage_records(occurred_at);

CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON source_chunks BEGIN
    INSERT INTO source_fts(rowid, chunk_id, content) VALUES (new.rowid, new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON source_chunks BEGIN
    DELETE FROM source_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF content ON source_chunks BEGIN
    UPDATE source_fts SET content = new.content, chunk_id = new.id WHERE rowid = old.rowid;
END;
"""


class BufferedCursor:
    """Compatibility cursor that never exposes a live cross-thread statement."""

    def __init__(self, rows: list[Any] = (), *, lastrowid: int | None = None, rowcount: int = -1):
        self._rows = iter(rows)
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return next(self._rows, None)

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self):
        return self._rows


class ConnectionFacade:
    """Legacy repository reads use committed readers, never the writer handle."""

    def __init__(self, database: Database):
        self.database = database

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> BufferedCursor:
        if sql.lstrip().upper().startswith(("SELECT", "WITH", "EXPLAIN", "PRAGMA")):
            return BufferedCursor(self.database.query(sql, parameters))
        return self.database.execute(sql, parameters)

    def executemany(self, sql: str, parameters) -> BufferedCursor:
        with self.database.transaction() as connection:
            cursor = connection.executemany(sql, parameters)
            return BufferedCursor(lastrowid=cursor.lastrowid, rowcount=cursor.rowcount)

    def commit(self) -> None:
        # Compatibility writes above are already atomic and committed.
        return None


class Database:
    def __init__(self, path: Path | str, passphrase: str, *, allow_plaintext: bool = False) -> None:
        self.path = str(path)
        self.passphrase = passphrase
        self.allow_plaintext = allow_plaintext or os.environ.get("EMAIL_READER_TESTING") == "1"
        self._connection: Any | None = None
        self._lock = threading.RLock()
        self._readers_lock = threading.Lock()
        self._lifecycle = threading.Condition()
        self._active_reads = 0
        self._readers: list[Any] = []
        self._local = threading.local()
        self._facade = ConnectionFacade(self)
        self._closed = False

    def open(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.allow_plaintext:
            connection = sqlite3.connect(self.path, check_same_thread=False)
            row_factory = sqlite3.Row
        else:
            connection, row_factory = self._open_encrypted()
        connection.row_factory = row_factory
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA secure_delete = ON")
        self._connection = connection
        self._closed = False
        self._connection.executescript(SCHEMA)
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "local_unread" not in columns:
            self._connection.execute("ALTER TABLE messages ADD COLUMN local_unread INTEGER")
        if "provider_revision" not in columns:
            self._connection.execute("ALTER TABLE messages ADD COLUMN provider_revision TEXT")
        if "source_complete" not in columns:
            self._connection.execute(
                "ALTER TABLE messages ADD COLUMN source_complete INTEGER NOT NULL DEFAULT 1"
            )
            self._connection.execute(
                "UPDATE messages SET source_complete=0 WHERE body_text='' AND body_html='' AND NOT EXISTS(SELECT 1 FROM attachments a WHERE a.message_id=messages.id)"
            )
        if "content_fingerprint" not in columns:
            self._connection.execute("ALTER TABLE messages ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT ''")
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_incomplete ON messages(account_id,source_complete)")
        attachment_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(attachments)")
        }
        if "content_id" not in attachment_columns:
            self._connection.execute(
                "ALTER TABLE attachments ADD COLUMN content_id TEXT NOT NULL DEFAULT ''"
            )
        if "content_location" not in attachment_columns:
            self._connection.execute(
                "ALTER TABLE attachments ADD COLUMN content_location TEXT NOT NULL DEFAULT ''"
            )
        if "content_fingerprint" not in attachment_columns:
            self._connection.execute("ALTER TABLE attachments ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT ''")
        if "in_main_inbox" not in columns:
            self._connection.execute(
                "ALTER TABLE messages ADD COLUMN in_main_inbox INTEGER NOT NULL DEFAULT 1"
            )
        if "is_filtered_bulk" not in columns:
            self._connection.execute(
                "ALTER TABLE messages ADD COLUMN is_filtered_bulk INTEGER NOT NULL DEFAULT 0"
            )
        if "mail_category" not in columns:
            self._connection.execute(
                "ALTER TABLE messages ADD COLUMN mail_category TEXT NOT NULL DEFAULT 'primary'"
            )
            self._connection.execute(
                "UPDATE messages SET mail_category='' "
                "WHERE in_main_inbox=0 OR is_sent=1 OR is_draft=1"
            )
        self._connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_messages_mailbox ON messages(
               account_id, in_main_inbox, is_sent, is_draft, mail_category, source_state
               )"""
        )
        # Keep navigation scans off the large encrypted body/HTML records.
        # Only the final visible page needs sender, recipients and snippet data.
        self._connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_messages_navigation ON messages(
                source_state,is_filtered_bulk,in_main_inbox,mail_category,
                thread_id,received_at DESC,id,local_unread,is_unread,is_sent,is_draft,account_id
            )"""
        )

    def _open_encrypted(self) -> tuple[Any, Any]:
        try:
            from sqlcipher3 import dbapi2 as sqlcipher

            connection = sqlcipher.connect(self.path, check_same_thread=False)
            row_factory = sqlcipher.Row
            escaped = self.passphrase.replace("'", "''")
            connection.execute(f"PRAGMA key = '{escaped}'")
            connection.execute("PRAGMA cipher_memory_security = ON")
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except ImportError as exc:
            raise RuntimeError(
                "SQLCipher is required. Install the 'sqlcipher' project extra; "
                "plaintext fallback is disabled for production."
            ) from exc
        return connection, row_factory

    @property
    def connection(self) -> Any:
        return self._facade

    def _writer(self) -> Any:
        if self._closed:
            raise RuntimeError("The private database is closed")
        if self._connection is None:
            self.open()
        return self._connection

    def _reader(self) -> Any:
        if self._closed and not getattr(self._local, "read_depth", 0):
            raise RuntimeError("The private database is closed")
        if self.path == ":memory:" or getattr(self._local, "in_transaction", False):
            return self._writer()
        reader = getattr(self._local, "reader", None)
        if reader is None:
            if self.allow_plaintext:
                reader = sqlite3.connect(self.path, check_same_thread=False)
                reader.row_factory = sqlite3.Row
            else:
                reader, row_factory = self._open_encrypted()
                reader.row_factory = row_factory
            reader.execute("PRAGMA query_only = ON")
            reader.execute("PRAGMA busy_timeout = 1500")
            self._local.reader = reader
            with self._readers_lock:
                self._readers.append(reader)
        return reader

    @contextmanager
    def _read_operation(self) -> Iterator[None]:
        # Closing never destroys a SQLite handle underneath an executing native
        # statement. A snapshot owns a lease until its final rollback completes.
        with self._lifecycle:
            depth = getattr(self._local, "read_depth", 0)
            if self._closed and not depth:
                raise RuntimeError("The private database is closed")
            self._active_reads += 1
            self._local.read_depth = depth + 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._local.read_depth -= 1
                self._active_reads -= 1
                self._lifecycle.notify_all()

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Keep a multi-query presentation read on one committed revision."""
        with self._read_operation(), self._snapshot():
            yield

    @contextmanager
    def _snapshot(self) -> Iterator[None]:
        # Test-only in-memory storage cannot use independent WAL readers.
        if self.path == ":memory:" or getattr(self._local, "in_transaction", False):
            with self._lock:
                yield
            return
        connection = self._reader()
        nested = connection.in_transaction
        if not nested:
            connection.execute("BEGIN")
        try:
            yield
        finally:
            if not nested:
                connection.rollback()

    def close(self) -> None:
        if getattr(self._local, "read_depth", 0):
            raise RuntimeError("Close storage after the current read snapshot has finished")
        with self._lifecycle:
            self._closed = True
            while self._active_reads:
                self._lifecycle.wait()
        # Writers are serialized independently; do not hold their lock while
        # waiting for read snapshots, which might be finishing a queued write.
        with self._lock:
            with self._readers_lock:
                for reader in self._readers:
                    reader.close()
                self._readers.clear()
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._local = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._lock:
            connection = self._writer()
            if getattr(self._local, "in_transaction", False):
                yield connection
                return
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._local.in_transaction = True
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._local.in_transaction = False

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        with self._lock:
            cursor = self._writer().execute(sql, parameters)
            rows = cursor.fetchall() if cursor.description else []
            result = BufferedCursor(rows, lastrowid=cursor.lastrowid, rowcount=cursor.rowcount)
            if not getattr(self._local, "in_transaction", False):
                self._writer().commit()
            return result

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[Any]:
        with self._read_operation():
            if self.path == ":memory:" or getattr(self._local, "in_transaction", False):
                with self._lock:
                    return list(self._reader().execute(sql, parameters).fetchall())
            return list(self._reader().execute(sql, parameters).fetchall())

    def iter_query(self, sql: str, parameters: Sequence[Any] = (), *, batch_size: int = 128):
        """Bounded worker-only scans, without buffering every embedding/body in RAM."""
        with self.read_snapshot():
            cursor = self._reader().execute(sql, parameters)
            try:
                while rows := cursor.fetchmany(batch_size):
                    yield from rows
            finally:
                cursor.close()

    def scalar(self, sql: str, parameters: Sequence[Any] = (), default: Any = None) -> Any:
        rows = self.query(sql, parameters)
        return rows[0][0] if rows else default

    def integrity_check(self) -> bool:
        # FTS5 integrity diagnostics can retain cached aggregate state on a
        # reader previously checked before an external commit. Use the writer
        # under its serialization lock; ordinary reads stay on independent WAL
        # snapshots. Backup verification uses a fresh destination connection.
        with self._lock:
            rows = self._writer().execute("PRAGMA integrity_check").fetchall()
            return len(rows) == 1 and rows[0][0] == "ok"

    def backup_to(self, target: Path, *, should_cancel=None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.allow_plaintext:
            output = sqlite3.connect(str(target))
        else:
            try:
                from sqlcipher3 import dbapi2 as sqlcipher

                output = sqlcipher.connect(str(target))
                escaped = self.passphrase.replace("'", "''")
                output.execute(f"PRAGMA key = '{escaped}'")
            except ImportError:
                raise
        try:
            # A separate WAL reader permits bounded backup pages while import writes continue.
            with self.read_snapshot(), output:
                def progress(_status, _remaining, _total):
                    if should_cancel and should_cancel():
                        raise RuntimeError("Backup postponed safely")
                self._reader().backup(output, pages=128, sleep=0.01, progress=progress)
            if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Encrypted backup failed its integrity check")
        finally:
            output.close()
        target.chmod(0o600)

    def verify_backup(self, target: Path) -> bool:
        if self.allow_plaintext:
            connection = sqlite3.connect(str(target))
        else:
            from sqlcipher3 import dbapi2 as sqlcipher
            connection = sqlcipher.connect(str(target))
            escaped = self.passphrase.replace("'", "''")
            connection.execute(f"PRAGMA key = '{escaped}'")
        try:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except Exception:
            return False
        finally:
            connection.close()

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()
