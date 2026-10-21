"""A full-sized fictional inbox must mark a conversation read promptly."""

from time import perf_counter
from types import SimpleNamespace

from PySide6.QtCore import QObject

from email_reader.controller import AppController
from email_reader.mail_images import MailImageProvider


def test_open_marks_read_and_updates_counts_within_one_second(repository):
    connection = repository.db.connection
    stamp = repository.db.now()
    size = 15_000
    connection.executemany(
        "INSERT INTO threads(id,account_id,provider_thread_id,subject,latest_at) "
        "VALUES(?,'account-1',?,'Fictional inbox item',?)",
        ((f"thread-{index:05d}", f"provider-{index:05d}", stamp) for index in range(size)),
    )
    connection.executemany(
        """INSERT INTO messages(
            id,thread_id,account_id,provider_id,subject,sent_at,received_at,
            is_unread,created_at
        ) VALUES(?,?,'account-1',?,'Fictional inbox item',?,?,1,?)""",
        (
            (f"message-{index:05d}", f"thread-{index:05d}", f"message-{index:05d}",
             stamp, stamp, stamp)
            for index in range(size)
        ),
    )
    connection.commit()

    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(repository=repository)
    controller.mail_images = MailImageProvider(repository.blobs)
    controller._threads = repository.list_threads(limit=200)
    controller._selected_account = ""
    controller._selected_mailbox = "inbox"
    controller._selected_category = "primary"
    controller._selected_thread = {}
    controller._keyword_query = ""
    target = controller._threads[0]["id"]
    provider_message = "message-" + target.removeprefix("thread-")
    assert controller._threads[0]["is_unread"] == 1

    started = perf_counter()
    controller.selectThread(target)
    elapsed = perf_counter() - started

    assert elapsed < 1.0, f"Open-to-local-read took {elapsed:.3f} seconds"
    assert controller._threads[0]["is_unread"] == 0
    assert controller.mailboxes[0]["unread"] == size - 1
    assert repository.db.scalar("SELECT is_unread FROM messages WHERE id=?", (provider_message,)) == 1
