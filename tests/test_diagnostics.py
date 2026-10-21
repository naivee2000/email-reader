import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import email_reader.diagnostics as diagnostics
from email_reader.app import build_parser
from email_reader.config import AppSettings
from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind
from email_reader.database import Database
from email_reader.diagnostics import (
    actionable_reply_thread_ids,
    audit_observed_reply_snapshot,
    benchmark_observed_replies,
    benchmark_work_path,
    format_reply_review_case,
    observed_reply_intent,
    observed_reply_pairs,
    prune_observed_reply_snapshot,
    reply_cohort_availability,
    reply_cohort_case_text,
    reply_references_resolve,
    reply_result_shape,
    save_observed_reply_intent,
)


def test_sync_health_contains_only_aggregate_dates_and_counts(repository):
    now = datetime.now(UTC)
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="private-id", provider_thread_id="private-thread",
        subject="Private subject", sender_email="private@example.com",
        to=["owner@example.com"], sent_at=now, received_at=now,
        body_text="Private body",
    ))
    context = SimpleNamespace(repository=repository, database=repository.db)
    report = diagnostics.sync_health(context)
    rows = report["accounts"][0]["recent_message_counts"]
    assert sum(row["count"] for row in rows) == 1
    serialized = json.dumps(report)
    for private in ("private-id", "Private subject", "private@example.com", "Private body", "owner@example.com"):
        assert private not in serialized
    repository.db.execute("UPDATE messages SET is_filtered_bulk=1 WHERE id=?", (message_id,))
    rows = diagnostics.sync_health(context)["accounts"][0]["recent_message_counts"]
    assert all(not row["visible"] for row in rows)


def test_targeted_import_inspection_is_read_only_until_recovery_requested(repository, monkeypatch):
    from email.message import EmailMessage

    import email_reader.providers.apple_mail as bridge
    source = EmailMessage()
    source["Message-ID"] = "<campus-event@example.edu>"
    source["From"] = "Events <private@example.edu>"
    source["Subject"] = "PSD happy hour"
    source["List-ID"] = "private-list.example.edu"
    source.set_content("Private event body")
    info = bridge.AppleMailAccountInfo("Fictional", "owner@example.com", ProviderKind.GMAIL, ("INBOX",))
    monkeypatch.setattr(bridge, "cached_apple_mail_accounts", lambda *args: ({info.email: info}, True))
    monkeypatch.setattr(bridge.AppleMailProvider, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [{"internalId": "1", "messageId": "campus-event@example.edu", "subject": "PSD happy hour",
                  "received": datetime.now(UTC).isoformat()}],
    })
    monkeypatch.setattr(bridge.AppleMailProvider, "_fetch_mailbox_sources", lambda *args: [source.as_string()])
    context = SimpleNamespace(repository=repository, database=repository.db)
    report = diagnostics.inspect_import(context, "happy hour", account_number=1)
    match = report["accounts"][0]["matches"][0]
    assert match["list_id_present"] and not match["stored_locally"]
    assert repository.db.scalar("SELECT COUNT(*) FROM messages") == 0
    assert "private@example.edu" not in json.dumps(report)
    report = diagnostics.inspect_import(context, "happy hour", recover=True, account_number=1)
    assert report["accounts"][0]["matches"][0]["recovered_locally"]
    assert repository.db.scalar("SELECT mail_category FROM messages") == "updates"
    assert repository.db.scalar("SELECT COUNT(*) FROM messages") == 1
    diagnostics.inspect_import(context, "happy hour", recover=True, account_number=1)
    assert repository.db.scalar("SELECT COUNT(*) FROM messages") == 1


def test_reply_benchmark_label_preserves_prior_runs():
    recovery = Path("/private/recovery")
    assert benchmark_work_path(recovery, "after") == recovery / "reply-benchmark-after.db"
    assert benchmark_work_path(recovery, "after", "installed-20260924") == (
        recovery / "reply-benchmark-after-installed-20260924.db"
    )


def test_reply_benchmark_effort_is_diagnostic_only():
    args = build_parser().parse_args(
        ["--benchmark-replies", "after", "--benchmark-effort", "none"]
    )
    assert args.benchmark_effort == "none"
    assert build_parser().parse_args([]).benchmark_effort == "low"
    assert not build_parser().parse_args([]).benchmark_compact
    assert build_parser().parse_args(["--benchmark-compact"]).benchmark_compact
    assert build_parser().parse_args([]).benchmark_cohort == "legacy"
    assert build_parser().parse_args(
        ["--benchmark-cohort", "actionable"]
    ).benchmark_cohort == "actionable"
    assert build_parser().parse_args(
        ["--benchmark-cohort", "recent"]
    ).benchmark_cohort == "recent"
    assert build_parser().parse_args(
        ["--benchmark-cohort", "recent-dialogue"]
    ).benchmark_cohort == "recent-dialogue"
    assert build_parser().parse_args(
        ["--reply-cohort-availability"]
    ).reply_cohort_availability
    assert build_parser().parse_args(
        ["--benchmark-cohort", "observed"]
    ).benchmark_cohort == "observed"
    assert build_parser().parse_args(
        ["--review-reply-cohort-case", "25", "--benchmark-cohort", "observed"]
    ).review_reply_cohort_case == 25
    assert build_parser().parse_args(
        ["--audit-observed-snapshot-case", "25"]
    ).audit_observed_snapshot_case == 25
    assert build_parser().parse_args(
        ["--review-reply-cohort-case", "2", "--benchmark-cohort", "observed",
         "--annotate-observed-intent"]
    ).annotate_observed_intent
    assert build_parser().parse_args(
        ["--benchmark-replies", "after", "--benchmark-cohort", "observed",
         "--benchmark-require-observed-intents"]
    ).benchmark_require_observed_intents


def test_reply_cohort_availability_releases_only_aggregate_flags(repository, tmp_path):
    moment = datetime.now(UTC) - timedelta(days=2)
    for index in range(10):
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=f"prior-{index}",
                provider_thread_id=f"review-{index}",
                subject=f"Review {index}",
                sender_email="owner@example.com",
                is_sent=True,
                body_text="Could you send me the meeting details?",
                sent_at=moment - timedelta(days=1),
            ),
        )
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=f"reply-{index}",
                provider_thread_id=f"review-{index}",
                subject=f"Review {index}",
                sender_email="person@example.net",
                body_text="Thanks for asking. The meeting is Thursday at two. " * 2,
                sent_at=moment,
            ),
        )
    repository.db.backup_to(tmp_path / "reply-benchmark-seed.db")
    context = SimpleNamespace(
        paths=SimpleNamespace(recovery=tmp_path),
        database=repository.db,
        repository=repository,
    )
    assert reply_cohort_availability(context) == {
        "recent_incoming": True,
        "recent_dialogue": True,
        "historical_dialogue": True,
        "observed_user_replies": False,
        "observed_user_reply_count": 0,
    }


def test_observed_reply_pairs_require_a_real_following_sent_message(repository, tmp_path):
    moment = datetime.now(UTC) - timedelta(days=2)
    for index in range(10):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"request-{index}",
            provider_thread_id=f"answered-{index}",
            subject=f"Question {index}",
            sender_email="person@example.net",
            body_text="Would Thursday afternoon work for your meeting? Please let me know. " * 2,
            sent_at=moment + timedelta(minutes=index),
            received_at=moment + timedelta(minutes=index),
        ))
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"answer-{index}",
            provider_thread_id=f"answered-{index}",
            subject=f"Question {index}",
            sender_email="owner@example.com",
            is_sent=True,
            body_text="Thursday afternoon works for me. Thank you for checking.",
            sent_at=moment + timedelta(minutes=index, seconds=30),
            received_at=moment + timedelta(minutes=index, seconds=30),
        ))
    pairs = observed_reply_pairs(repository.db, ["account-1"])
    assert len(pairs) == 10
    assert len({thread_id for thread_id, _, _ in pairs}) == 10
    repository.db.backup_to(tmp_path / "reply-benchmark-seed.db")
    context = SimpleNamespace(
        paths=SimpleNamespace(recovery=tmp_path),
        database=repository.db,
        repository=repository,
    )
    review = reply_cohort_case_text(context, "observed", 1)
    assert reply_cohort_availability(context)["observed_user_reply_count"] == 10
    assert "ACTUAL USER-APPROVED REPLY" in review
    assert "Thursday afternoon works for me" in review
    assert "Would Thursday afternoon work" in review
    assert audit_observed_reply_snapshot(context, 1) == {
        "case": 1,
        "latest_is_incoming": True,
        "held_out_absent": True,
        "no_future_messages": True,
        "no_orphan_search_chunks": True,
        "no_cached_tone": True,
        "integrity": True,
    }


def test_observed_snapshot_excludes_reply_future_mail_and_derived_content(repository):
    moment = datetime(2026, 8, 1, 12, tzinfo=UTC)
    repository.upsert_account(Account(
        id="account-2", provider=ProviderKind.GMAIL, email="second@example.com"
    ))
    incoming_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="incoming", provider_thread_id="exchange", subject="Question",
        sender_email="person@example.net", body_text="Can we meet on Friday? " * 5,
        sent_at=moment, received_at=moment,
        attachments=[AttachmentPayload(provider_id="initial-file", filename="agenda.txt",
            mime_type="text/plain", content=b"agenda")],
    ))
    sent_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="sent", provider_thread_id="exchange", subject="Question",
        internet_message_id="<held-out@example.com>", sender_email="owner@example.com",
        to=["person@example.net"], is_sent=True,
        body_text="Friday works for me. Please use the confidential plan.",
        sent_at=moment + timedelta(minutes=2), received_at=moment + timedelta(minutes=2),
        attachments=[AttachmentPayload(provider_id="future-file", filename="future.txt",
            mime_type="text/plain", content=b"confidential plan")],
    ))
    mirrored_id = repository.ingest_message("account-2", MessagePayload(
        provider_id="mirrored-sent", provider_thread_id="mirror", subject="Question",
        internet_message_id="<held-out@example.com>", sender_email="owner@example.com",
        is_sent=True, body_text="Friday works for me. Please use the confidential plan.",
        sent_at=moment + timedelta(minutes=1), received_at=moment + timedelta(minutes=1),
    ))
    repository.ingest_message("account-1", MessagePayload(
        provider_id="later", provider_thread_id="later", subject="Later",
        sender_email="person@example.net", body_text="A later message with another clue.",
        sent_at=datetime(2026, 8, 1, 8, 4, tzinfo=timezone(timedelta(hours=-4))),
        received_at=datetime(2026, 8, 1, 8, 4, tzinfo=timezone(timedelta(hours=-4))),
    ))
    repository.save_tone_note("account-1", "person@example.net", "future style note", [sent_id])
    database = repository.db
    assert database.scalar("SELECT COUNT(*) FROM tone_examples WHERE message_id=?", (sent_id,)) == 1
    assert database.scalar("SELECT COUNT(*) FROM source_chunks WHERE source_id=?", (sent_id,)) >= 1
    reference_now = prune_observed_reply_snapshot(database, incoming_id, sent_id)
    assert reference_now == moment + timedelta(minutes=2)
    assert database.scalar("SELECT 1 FROM messages WHERE id=?", (incoming_id,)) == 1
    assert database.scalar("SELECT 1 FROM messages WHERE id=?", (sent_id,)) is None
    assert database.scalar("SELECT 1 FROM messages WHERE id=?", (mirrored_id,)) is None
    assert database.scalar("SELECT COUNT(*) FROM messages") == 1
    assert database.scalar("SELECT COUNT(*) FROM attachments") == 1
    assert database.scalar("SELECT COUNT(*) FROM source_chunks WHERE source_id=?", (sent_id,)) == 0
    assert database.scalar("SELECT COUNT(*) FROM tone_examples") == 0
    assert database.scalar("SELECT COUNT(*) FROM tone_notes") == 0
    assert database.scalar("SELECT COUNT(*) FROM source_fts WHERE content MATCH 'confidential'") == 0
    assert database.scalar("SELECT COUNT(*) FROM threads") == 1
    assert database.integrity_check()


@pytest.mark.parametrize("require_intents", [False, True])
def test_observed_benchmark_uses_ten_answer_free_historical_snapshots(
    repository, tmp_path, monkeypatch, require_intents,
):
    start = datetime(2026, 9, 1, 12, tzinfo=UTC)
    for index in range(16):
        moment = start - timedelta(days=index)
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"incoming-{index}", provider_thread_id=f"exchange-{index}",
            subject=f"Question {index}", sender_email=f"person{index}@example.net",
            body_text="Would Thursday work for our meeting? Please let me know. " * 2,
            sent_at=moment, received_at=moment,
        ))
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"sent-{index}", provider_thread_id=f"exchange-{index}",
            subject=f"Question {index}", sender_email="owner@example.com",
            to=[f"person{index}@example.net"], is_sent=True,
            body_text=f"Thursday works for me on case {index}. Thank you!",
            sent_at=moment + timedelta(minutes=1),
            received_at=moment + timedelta(minutes=1),
        ))
    repository.db.backup_to(tmp_path / "reply-benchmark-seed.db")
    seen: list[tuple[str, datetime, str]] = []

    class FakeAssistant:
        def __init__(self, current_repository, search, ai, settings):
            self.current_repository = current_repository
            self.last_reply_timings = {"generation": 0.25, "total": 0.3}

        def draft_reply(self, thread_id, user_instruction="", *, reference_now):
            thread = self.current_repository.get_thread(thread_id)
            assert thread is not None
            assert thread["messages"][-1]["is_sent"] == 0
            assert all(item["sent_at"] < reference_now.isoformat()
                       for item in thread["messages"])
            seen.append((thread_id, reference_now, user_instruction))
            source_id = thread["messages"][-1]["id"]
            result = {"decision_detected": False, "candidates": [
                {"label": label, "body": f"Reply {label}", "stance": "", "assumption": "",
                 "supporting_source_ids": [source_id], "tone_example_ids": []}
                for label in ("Concise", "Warm", "Detailed")
            ]}
            return SimpleNamespace(candidates=result["candidates"],
                                   model_dump=lambda mode: result)

    monkeypatch.setattr(diagnostics, "OpenAIService", lambda *args: SimpleNamespace())
    monkeypatch.setattr(diagnostics, "HybridSearch", lambda *args: SimpleNamespace())
    monkeypatch.setattr(diagnostics, "AssistantService", FakeAssistant)
    context = SimpleNamespace(
        paths=SimpleNamespace(recovery=tmp_path), database=repository.db,
        repository=repository, vault=SimpleNamespace(),
        settings=AppSettings(response_model="test-model"),
        openai=SimpleNamespace(usage=SimpleNamespace(), codex_executable=""),
    )
    if require_intents:
        with pytest.raises(ValueError, match="private intent"):
            benchmark_observed_replies(
                context, "after", "fictional", "low", False, True
            )
        for case_number in diagnostics.OBSERVED_BENCHMARK_CASES:
            save_observed_reply_intent(
                context, case_number, f"I prefer Thursday in fictional case {case_number}."
            )
        assert observed_reply_intent(context, 2) == "I prefer Thursday in fictional case 2."
    benchmark_observed_replies(
        context, "after", "fictional", "low", False, require_intents
    )
    assert len(seen) == 10
    assert all(left[1] > right[1] for left, right in zip(seen, seen[1:], strict=False))
    assert all(bool(item[2]) == require_intents for item in seen)
    results = Database(benchmark_work_path(tmp_path, "after", "fictional"),
                       repository.db.passphrase, allow_plaintext=True)
    results.open()
    try:
        saved = json.loads(results.scalar(
            "SELECT value FROM app_settings WHERE key='benchmark_reply_results'"
        ))
        assert saved["cohort"] == "observed"
        assert saved["required_intents"] == require_intents
        assert len(saved["runs"]) == 10
        assert all(run["intent_supplied"] == require_intents for run in saved["runs"])
        assert all(run["snapshot_references_resolve"] for run in saved["runs"])
        assert all(run["stage_seconds"] == {"generation": 0.25, "total": 0.3}
                   for run in saved["runs"])
    finally:
        results.close()


def test_actionable_reply_cohort_deduplicates_mirrored_mail(repository):
    repository.upsert_account(Account(
        id="account-2", provider=ProviderKind.GMAIL, email="second@example.com"
    ))
    moment = datetime.now(UTC) - timedelta(days=2)
    for account_id in ("account-1", "account-2"):
        repository.ingest_message(account_id, MessagePayload(
            provider_id="mirror-" + account_id,
            provider_thread_id="mirror",
            internet_message_id="<same-message@example.net>",
            sender_email="person@example.net",
            subject="One invitation",
            body_text="Could you tell me whether Thursday at two works for our meeting? " * 2,
            sent_at=moment + timedelta(minutes=1),
        ))
    for index in range(10):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"unique-{index}",
            provider_thread_id=f"unique-{index}",
            sender_email="other@example.net",
            subject=f"Invitation {index}",
            body_text="Could you tell me whether Thursday at three works for our meeting? " * 2,
            sent_at=moment - timedelta(minutes=index),
        ))
    selected = actionable_reply_thread_ids(
        repository.db, ["account-1", "account-2"], 30
    )
    mirrored = [identifier for identifier in selected if repository.db.scalar(
        "SELECT provider_thread_id FROM threads WHERE id=?", (identifier,)
    ) == "mirror"]
    assert len(selected) == 10
    assert len(mirrored) == 1


def test_actionable_reply_cohort_requires_latest_non_draft_message_to_be_incoming(repository):
    start = datetime.now(UTC) - timedelta(days=10)
    for index in range(10):
        moment = start + timedelta(minutes=index)
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=f"incoming-{index}",
                provider_thread_id=f"pending-{index}",
                sender_email="sender@example.net",
                body_text="Please reply with the schedule and any important details. " * 2,
                sent_at=moment,
                received_at=moment,
            ),
        )
    # An unsent draft is not a new request and must not hide the incoming mail.
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="draft-on-pending",
            provider_thread_id="pending-0",
            sender_email="owner@example.com",
            body_text="A draft that has not been sent.",
            is_draft=True,
            sent_at=start + timedelta(days=2),
            received_at=start + timedelta(days=2),
        ),
    )
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="old-inbound",
            provider_thread_id="already-answered",
            sender_email="sender@example.net",
            body_text="Would you like to attend the session next week? " * 2,
            sent_at=start,
            received_at=start,
        ),
    )
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="new-outbound",
            provider_thread_id="already-answered",
            sender_email="owner@example.com",
            to=["sender@example.net"],
            body_text="Thank you. I already answered the session invitation.",
            is_sent=True,
            sent_at=start + timedelta(days=1),
            received_at=start + timedelta(days=1),
        ),
    )
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="unmonitored-notice",
            provider_thread_id="unmonitored-notice",
            sender_email="forms-receipts-noreply@example.net",
            body_text="This is an automated security notice. Replies are not monitored. " * 2,
            sent_at=start + timedelta(days=1),
            received_at=start + timedelta(days=1),
        ),
    )
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="weekly-notification",
            provider_thread_id="weekly-notification",
            sender_email="notifications@example.net",
            body_text=("You received this email because you are participating in a class. "
                       "Change your notification settings to turn off email notifications. " * 2),
            sent_at=start + timedelta(days=1),
            received_at=start + timedelta(days=1),
        ),
    )
    identifiers = actionable_reply_thread_ids(repository.db, ["account-1"])
    assert len(identifiers) == len(set(identifiers)) == 10
    assert repository.db.scalar(
        "SELECT provider_thread_id FROM threads WHERE id=?", (identifiers[0],)
    ) != "already-answered"
    assert "already-answered" not in {
        repository.db.scalar("SELECT provider_thread_id FROM threads WHERE id=?", (item,))
        for item in identifiers
    }
    assert "unmonitored-notice" not in {
        repository.db.scalar("SELECT provider_thread_id FROM threads WHERE id=?", (item,))
        for item in identifiers
    }
    assert "weekly-notification" not in {
        repository.db.scalar("SELECT provider_thread_id FROM threads WHERE id=?", (item,))
        for item in identifiers
    }
    assert "pending-0" in {
        repository.db.scalar("SELECT provider_thread_id FROM threads WHERE id=?", (item,))
        for item in identifiers
    }
    assert len(actionable_reply_thread_ids(repository.db, ["account-1"], 30)) == 10
    with pytest.raises(ValueError, match="Ten currently actionable"):
        actionable_reply_thread_ids(repository.db, ["account-1"], 1)
    with pytest.raises(ValueError, match="Ten currently actionable"):
        actionable_reply_thread_ids(repository.db, ["account-1"], 30, True)


def test_recent_dialogue_cohort_requires_prior_sent_mail(repository):
    now = datetime.now(UTC)
    for index in range(10):
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=f"outgoing-dialogue-{index}",
                provider_thread_id=f"dialogue-{index}",
                subject=f"Dialogue {index}",
                sender_email="owner@example.com",
                to=["maya@example.net"],
                is_sent=True,
                body_text="Could you tell me when the meeting starts?",
                sent_at=now - timedelta(days=3),
            ),
        )
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=f"incoming-dialogue-{index}",
                provider_thread_id=f"dialogue-{index}",
                subject=f"Dialogue {index}",
                sender_email="maya@example.net",
                body_text="Thanks for writing. The meeting starts Thursday at two in the afternoon. " * 2,
                sent_at=now - timedelta(days=2),
            ),
        )
    identifiers = actionable_reply_thread_ids(repository.db, ["account-1"], 30, True)
    assert len(identifiers) == len(set(identifiers)) == 10
    assert {
        repository.db.scalar("SELECT provider_thread_id FROM threads WHERE id=?", (item,))
        for item in identifiers
    } == {f"dialogue-{index}" for index in range(10)}


def test_reply_result_shape_flags_repeated_or_missing_choices_without_disclosing_content():
    healthy = {
        "decision_detected": False,
        "candidates": [
            {"label": "Concise", "stance": "Reply", "body": "Thanks. I can meet Tuesday."},
            {"label": "Warm", "stance": "Reply", "body": "Thank you! Tuesday works well."},
            {"label": "Detailed", "stance": "Reply", "body": "Thanks for reaching out. I can meet Tuesday."},
        ],
    }
    shape = reply_result_shape(healthy)
    assert shape["three_candidates"] and shape["distinct_bodies"]
    assert shape["variant_labels"] and shape["distinct_labels"]
    assert "Thanks" not in str(shape)
    broken = {
        "decision_detected": True,
        "candidates": [
            {"label": "Yes", "stance": "Accept", "body": "[SOURCE:fake] Yes."},
            {"label": "No", "stance": "Accept", "body": "[SOURCE:fake] Yes."},
        ],
    }
    shape = reply_result_shape(broken)
    assert not shape["three_candidates"]
    assert not shape["distinct_bodies"]
    assert not shape["decision_stances"]
    assert shape["source_markers_in_body"]


def test_review_reply_parser_requires_two_labels():
    args = build_parser().parse_args(
        ["--review-reply-benchmarks", "dedup-20260924", "none-20260924"]
    )
    assert args.review_reply_benchmarks == ["dedup-20260924", "none-20260924"]
    assert build_parser().parse_args(["--review-reply-case", "6"]).review_reply_case == 6
    assert build_parser().parse_args(
        ["--review-reply-cohort-case", "3", "--benchmark-cohort", "recent"]
    ).review_reply_cohort_case == 3


def test_private_reply_case_formatter_uses_new_content_and_omits_unsent_drafts():
    thread = {
        "subject": "Example invitation",
        "account_email": "owner@example.com",
        "messages": [
            {
                "is_draft": False,
                "sender_name": "Maya",
                "sender_email": "maya@example.net",
                "sent_at": "2026-09-01",
                "new_content": "Choose Tuesday or Thursday.",
                "body_text": "Choose Tuesday or Thursday.\nOLD_QUOTED_TEXT",
            },
            {
                "is_draft": True,
                "sender_name": "Owner",
                "sender_email": "owner@example.com",
                "sent_at": "2026-09-02",
                "new_content": "UNSENT_PRIVATE_DRAFT",
                "body_text": "UNSENT_PRIVATE_DRAFT",
            },
        ],
    }
    older = {"decision_detected": True, "candidates": [
        {"label": "Yes", "stance": "Tuesday", "assumption": "If free", "body": "Tuesday works."}
    ]}
    newer = {"decision_detected": False, "candidates": [
        {"label": "Warm", "stance": "Reply", "assumption": "", "body": "Thanks for inviting me."}
    ]}
    text = format_reply_review_case(thread, older, newer, "low", "compact", 6)
    assert "Choose Tuesday or Thursday." in text
    assert "OLD_QUOTED_TEXT" not in text
    assert "UNSENT_PRIVATE_DRAFT" not in text
    assert "Tuesday works." in text and "Thanks for inviting me." in text


def test_reply_reference_check_rejects_missing_and_non_sent_tone_examples(repository):
    identifier = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="reply-source",
            provider_thread_id="reply-thread",
            sender_email="maya@example.net",
            body_text="A test message with enough content to create a source chunk.",
        ),
    )
    chunk = repository.db.scalar(
        "SELECT id FROM source_chunks WHERE source_type='message' AND source_id=?",
        (identifier,),
    )
    result = {"candidates": [{"supporting_source_ids": [chunk], "tone_example_ids": []}]}
    assert reply_references_resolve(repository.db, result, "account-1")
    assert not reply_references_resolve(repository.db, result, "another-account")
    result["candidates"][0]["supporting_source_ids"] = ["nonexistent"]
    assert not reply_references_resolve(repository.db, result, "account-1")
    result["candidates"][0]["supporting_source_ids"] = [chunk]
    result["candidates"][0]["tone_example_ids"] = [identifier]
    assert not reply_references_resolve(repository.db, result, "account-1")


@pytest.mark.parametrize("label", ["../escape", "UPPER", "space here", "-leading", "x" * 41])
def test_reply_benchmark_label_rejects_unsafe_paths(label):
    with pytest.raises(ValueError, match="short lowercase"):
        benchmark_work_path(Path("/private/recovery"), "after", label)
