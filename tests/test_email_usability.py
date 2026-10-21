import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject

from email_reader.ai import (
    AssistantService,
    CompactReplyChoice,
    CompactReplySet,
    FactList,
    OpenAIService,
    ToneNote,
)
from email_reader.config import AppSettings
from email_reader.contracts import (
    Account,
    Answer,
    DraftCandidate,
    DraftSet,
    MessagePayload,
    ProviderKind,
    Summary,
)
from email_reader.controller import AppController
from email_reader.drafts import initial_draft, reply_requires_instruction
from email_reader.search import SearchHit
from email_reader.text import message_new_content


def sent(repo, identifier="sent", recipient="Maya <maya@example.net>"):
    return repo.ingest_message(
        "account-1",
        MessagePayload(
            provider_id=identifier,
            provider_thread_id="conversation",
            sender_email="owner@example.com",
            to=[recipient],
            is_sent=True,
            body_text="Hi Maya,\n\nThanks for this. That works for me.\n\nBest,\nOwner",
        ),
    )


def test_reply_stage_timings_are_numeric_content_free_and_reset(repository, monkeypatch, capsys):
    sent(repository)
    thread_id = repository.list_threads(mailbox="sent")[0]["id"]
    ai = Mock()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[DraftCandidate(label=label, stance="Follow up", body=body)
                    for label, body in (("Concise", "Private body one."),
                                        ("Warm", "Private body two."),
                                        ("Detailed", "Private body three."))],
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    clock = iter(range(100))
    monkeypatch.setattr("email_reader.ai.time.perf_counter", lambda: next(clock))
    monkeypatch.setenv("EMAIL_READER_PROFILE_REPLY", "1")
    service.draft_reply(thread_id, "Private instruction: follow up about scheduling.")
    assert service.last_reply_timings == {
        "thread": 1, "tone": 1, "retrieval": 1, "prompt": 1,
        "generation": 1, "validation": 1, "total": 12,
    }
    output = capsys.readouterr().out.strip()
    assert json.loads(output.removeprefix("Reply components: ")) == service.last_reply_timings
    assert "Private" not in output and thread_id not in output
    with pytest.raises(KeyError, match="Thread not found"):
        service.draft_reply("unavailable-thread")
    assert service.last_reply_timings == {}  # Never reuse a prior operation's numbers.


@pytest.mark.parametrize(
    "schema_type", [DraftSet, CompactReplySet, Summary, Answer, FactList, ToneNote]
)
def test_entire_schema_is_strict(schema_type):
    schema = OpenAIService._strict_schema(schema_type)
    assert schema["type"] == "object"

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            assert "default" not in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_schema_failure_is_not_reported_as_login_and_no_private_output():
    error = OpenAIService._codex_error("invalid_json_schema private email body", "")
    assert "format" in error
    assert "private email body" not in error
    assert "signing in again will not fix" in error


@pytest.mark.parametrize("bad_cache", ["uncited", "foreign", "malformed"])
def test_summary_regenerates_unverifiable_cache_then_reuses_cited_result(repository, bad_cache):
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="summary-source", provider_thread_id="summary-thread",
        sender_email="maya@example.net", to=["owner@example.com"],
        subject="Meeting", body_text="The meeting is on Tuesday.",
    ))
    thread = repository.get_thread(repository.list_threads()[0]["id"])
    invalid = Summary(key_points=["The meeting is on Tuesday."], source_ids=[message_id])
    if bad_cache == "foreign":
        invalid.key_points = ["The meeting is on Tuesday. [SOURCE:01234567-89ab-cdef-0123-456789abcdef]"]
    repository.save_summary(thread["id"], thread["content_hash"],
                            "not-json" if bad_cache == "malformed" else invalid.model_dump_json(), "test")
    valid = Summary(key_points=[f"The meeting is on Tuesday. [SOURCE:{message_id}]"],
                    source_ids=[message_id])
    ai = Mock()
    ai.structured.return_value = valid
    service = AssistantService(repository, Mock(), ai, AppSettings())
    assert service.summarize_thread(thread["id"]) == valid
    instructions = ai.structured.call_args.kwargs["instructions"]
    assert "[SOURCE:<message UUID>]" in instructions
    assert "each bullet needs its own marker" in instructions
    assert "Return empty lists for unsupported sections" in instructions
    assert "present status is unverified" in instructions
    assert "do not claim the task is still due" in instructions
    assert service.summarize_thread(thread["id"]) == valid
    ai.structured.assert_called_once()


def test_dated_action_summary_replaces_old_cited_cache(repository):
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="discount-request", provider_thread_id="discount-thread",
        sender_email="agent@example.net", to=["owner@example.com"],
        subject="Course discount", body_text="Please return course proof within 14 days.",
    ))
    thread = repository.get_thread(repository.list_threads()[0]["id"])
    old = Summary(action_items=[f"Complete the course now. [SOURCE:{message_id}]"],
                  source_ids=[message_id])
    repository.save_summary(thread["id"], thread["content_hash"], old.model_dump_json(),
                            "old-summary-model")
    corrected = Summary(action_items=[
        f"The agent requested course proof within 14 days; current status is unverified. "
        f"[SOURCE:{message_id}]"], source_ids=[message_id])
    ai = Mock()
    ai.structured.return_value = corrected
    service = AssistantService(repository, Mock(), ai, AppSettings())
    assert service.summarize_thread(thread["id"]) == corrected
    assert service.summarize_thread(thread["id"]) == corrected
    ai.structured.assert_called_once()


def test_uncited_new_summary_is_not_cached_or_given_invented_citations(repository):
    message_id = sent(repository)
    thread = repository.get_thread(repository.db.scalar(
        "SELECT thread_id FROM messages WHERE id=?", (message_id,)
    ))
    invalid = Summary(key_points=["An unsupported claim."], source_ids=[message_id])
    ai = Mock()
    ai.structured.return_value = invalid
    service = AssistantService(repository, Mock(), ai, AppSettings())
    result = service.summarize_thread(thread["id"])
    assert result.key_points == ["An unsupported claim."]
    assert repository.cached_summary(thread["id"], thread["content_hash"]) is None


def test_tone_document_reused_across_requests_and_service_restart(repository, monkeypatch):
    source = sent(repository)
    ai = Mock()
    choices = DraftSet(
        decision_detected=False,
        tone_guidance="Brief, friendly; uses Hi and Best.",
        candidates=[
            DraftCandidate(label=label, stance="Accept", body=f"{label}: Thanks, that works.")
            for label in ["Concise", "Warm", "Detailed"]
        ],
    )
    compact = CompactReplySet(
        decision_detected=False,
        style_note="Brief, friendly; uses Hi and Best.",
        drafts=[
            CompactReplyChoice(label=label, stance="Accept", body=f"{label}: Thanks, that works.")
            for label in ["Concise", "Warm", "Detailed"]
        ],
    )
    ai.structured.side_effect = lambda kind, **kw: (
        ToneNote(guidance="Brief, friendly; uses Hi and Best.") if kind is ToneNote
        else compact if kind is CompactReplySet else choices
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    service.draft_new("account-1", ["maya@example.net"], "Thanks", "Accept")
    note = repository.cached_tone_note("account-1", "MAYA@example.net")
    assert note["tone_example_ids"] == [source]
    assert "friendly" in note["document"]
    monkeypatch.setattr(
        repository, "get_tone_examples", Mock(side_effect=AssertionError("Must not rescan"))
    )
    restarted = AssistantService(repository, search, ai, AppSettings())
    restarted.draft_new("account-1", ["maya@example.net"], "Thanks", "Accept")
    restarted.draft_reply(
        repository.list_threads(mailbox="sent")[0]["id"], "Follow up about the schedule"
    )
    assert len([call for call in ai.structured.call_args_list if call.args[0] is ToneNote]) == 0
    assert len(ai.structured.call_args_list) == 3  # one request per draft, no separate profile call
    assert "SAVED TONE NOTE" in ai.structured.call_args.kwargs["input_text"]


def test_default_reply_avoids_semantic_lookup_but_explicit_request_keeps_it(repository):
    message_id = repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="reply-request", provider_thread_id="reply-request",
            sender_email="maya@example.net", to=["owner@example.com"],
            body_text="Could you let me know whether Thursday works?",
            sent_at=datetime.now(UTC), received_at=datetime.now(UTC),
        )
    )
    thread_id = repository.db.scalar(
        "SELECT thread_id FROM messages WHERE id=?", (message_id,)
    )
    ai = Mock()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Check availability", body=f"{label}: Let me check.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    service.draft_reply(thread_id)
    assert search.search.call_args.kwargs["semantic"] is False
    assert search.search.call_args.kwargs["semantic_message_id"] == message_id
    service.draft_reply(thread_id, "Check the earlier schedule in other mail")
    assert search.search.call_args.kwargs["semantic"] is True
    assert search.search.call_args.kwargs["semantic_message_id"] is None


def test_cross_account_reply_uses_selected_identity_and_tone_but_original_evidence(repository):
    repository.upsert_account(Account(id="account-2", provider=ProviderKind.OUTLOOK,
                                      email="alternate@example.com", display_name="Alternate"))
    source = repository.ingest_message("account-1", MessagePayload(
        provider_id="source", provider_thread_id="source", sender_email="maya@example.net",
        to=["owner@example.com"], body_text="Could you confirm the Thursday appointment?",
    ))
    style = repository.ingest_message("account-2", MessagePayload(
        provider_id="style", provider_thread_id="style", sender_email="alternate@example.com",
        to=["maya@example.net"], is_sent=True,
        body_text="Hi Maya,\n\nHappy to help.\n\nBest,\nAlternate",
    ))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (source,))
    ai = Mock()
    ai.structured.return_value = DraftSet(decision_detected=False,
        tone_guidance="Warm and brief, with a friendly greeting.",
        candidates=[DraftCandidate(label=label, stance="Confirm availability",
            body=f"{label}: Thursday works.", supporting_source_ids=[source],
            tone_example_ids=[style]) for label in ("Concise", "Warm", "Detailed")])
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    result = service.draft_reply(thread_id, sending_account_id="account-2",
                                 recipients=["maya@example.net"], draft_subject="Re: Thursday")
    prompt = ai.structured.call_args.kwargs["input_text"]
    assert "SENDING IDENTITY: alternate@example.com" in prompt
    assert "TO: maya@example.net" in prompt
    assert "DRAFT SUBJECT: Re: Thursday" in prompt
    assert "Could you confirm the Thursday appointment?" in prompt
    assert "Happy to help" in prompt
    assert search.search.call_args.kwargs["account_id"] == "account-1"
    assert all(choice.supporting_source_ids == [source] and choice.tone_example_ids == [style]
               for choice in result.candidates)
    assert repository.cached_tone_note("account-2", "maya@example.net")
    assert repository.cached_tone_note("account-1", "maya@example.net") is None


def test_editor_cross_account_reply_keeps_thread_context(repository):
    repository.upsert_account(Account(id="account-2", provider=ProviderKind.OUTLOOK,
                                      email="alternate@example.com"))
    source = repository.ingest_message("account-1", MessagePayload(
        provider_id="source", provider_thread_id="source", sender_email="maya@example.net",
        to=["owner@example.com"], body_text="Please reply to this thread.",
    ))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (source,))
    assistant = Mock()
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(repository=repository, assistant=assistant)
    controller._accounts = repository.list_accounts()
    controller._editor_draft = initial_draft("reply", repository.get_thread(thread_id),
                                             controller._accounts)
    controller._busy = False
    controller._set_status = Mock()
    controller._run = lambda _label, work, _done, **_kwargs: work(lambda _update: None)
    form = {**controller.editorDraft, "account_id": "account-2"}
    controller.assistEditor(form, "Confirm Thursday")
    assistant.draft_reply.assert_called_once_with(
        thread_id, "Confirm Thursday", sending_account_id="account-2",
        recipients=["maya@example.net"], draft_subject=form["subject"],
    )
    assistant.draft_new.assert_not_called()


@pytest.mark.parametrize("operation", ["new", "reply"])
def test_rejected_draft_choices_do_not_cache_tone_note(repository, operation):
    sent(repository)
    ai = Mock()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        tone_guidance="Use an upbeat greeting and a brief sign-off.",
        candidates=[
            DraftCandidate(label=label, stance="Accept", body="Identical wording.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    with pytest.raises(ValueError, match="three distinct options"):
        if operation == "new":
            service.draft_new("account-1", ["maya@example.net"], "Thanks", "Accept")
        else:
            thread_id = repository.list_threads(mailbox="sent")[0]["id"]
            service.draft_reply(thread_id, "Follow up about the schedule")
    assert repository.cached_tone_note("account-1", "maya@example.net") is None


def test_reply_context_does_not_repeat_same_thread_message_search_hit(repository):
    reply_message = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="reply-source",
            provider_thread_id="reply-thread",
            sender_email="maya@example.net",
            body_text="DUPLICATE_KNOWLEDGE_MARKER Please reply about the schedule.",
        ),
    )
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="related-source",
            provider_thread_id="related-thread",
            sender_email="other@example.net",
            body_text="RELATED_KNOWLEDGE_MARKER The schedule is in the document.",
        ),
    )
    chunks = repository.db.query(
        "SELECT id,content FROM source_chunks WHERE source_type='message' ORDER BY content"
    )
    hits = [
        SearchHit(
            chunk_id=row["id"],
            content=row["content"],
            score=1.0,
            citation=repository.citation_for_chunk(row["id"]),
        )
        for row in chunks
    ]
    search = Mock()
    search.search.return_value = hits
    ai = Mock()
    ai.structured.return_value = CompactReplySet(
        decision_detected=False,
        drafts=[
            CompactReplyChoice(label=label, stance="Reply", body=f"{label}: Thanks for the note.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (reply_message,))
    AssistantService(
        repository, search, ai, AppSettings(compact_reply_experiment=True)
    ).draft_reply(thread_id)
    prompt = ai.structured.call_args.kwargs["input_text"]
    assert prompt.count("DUPLICATE_KNOWLEDGE_MARKER") == 1
    assert prompt.count("RELATED_KNOWLEDGE_MARKER") == 1


def test_reply_context_omits_only_quotes_already_present_in_thread():
    original = (
        "The workshop is on Thursday at 3 p.m. in the library meeting room, "
        "and the registration link is in the course portal."
    )
    incoming = "Thanks for the details. I will check the portal."

    def message(identifier, body, fresh, *, has_quote=False, draft=False):
        return {
            "id": identifier, "sender_name": "Maya", "sender_email": "maya@example.net",
            "sent_at": "2026-09-24T10:00:00+00:00", "subject": "Workshop",
            "body_text": body, "new_content": fresh,
            "has_quoted_history": has_quote, "is_draft": draft, "attachments": [],
        }

    copied = "On Thursday, Maya wrote:\n> " + original
    thread = {"messages": [
        message("m1", original, original),
        message("m2", incoming + "\n\n" + copied, incoming, has_quote=True),
    ]}
    context = AssistantService._reply_thread_context(thread)
    assert context.count(original) == 1
    assert "[SOURCE:m1]" in context and "[SOURCE:m2]" in context
    assert incoming in context
    assert thread["messages"][1]["body_text"].endswith(copied)

    # A quoted forward from outside the stored conversation is unique evidence.
    thread["messages"][1] = message(
        "m2", incoming + "\n\nOn Thursday, Maya wrote:\n> " + "Different details " * 12,
        incoming, has_quote=True,
    )
    assert "Different details" in AssistantService._reply_thread_context(thread)

    # Never trim a user's unfinished draft, even if it quotes a stored message.
    thread["messages"][1] = message(
        "m2", incoming + "\n\n" + copied, incoming, has_quote=True, draft=True,
    )
    assert copied in AssistantService._reply_thread_context(thread)


def test_sent_last_thread_requires_explicit_follow_up_and_never_revives_old_request(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="old-invitation",
            provider_thread_id="conversation",
            sender_email="maya@example.net",
            body_text="Would you like to attend the optional session next week?",
            sent_at=datetime.now(UTC) - timedelta(days=1),
        ),
    )
    outgoing_id = sent(repository, "already-answered")
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (outgoing_id,))
    ai = Mock()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Follow up", body=f"{label}: Just checking in.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    with pytest.raises(ValueError, match="already sent the latest message"):
        service.draft_reply(thread_id)
    ai.structured.assert_not_called()
    service.draft_reply(thread_id, "Follow up about the schedule only")
    prompt = ai.structured.call_args.kwargs["input_text"]
    assert "Sent by the account owner" in prompt
    assert "CURRENT LOCAL DATE:" in prompt
    assert "SENDING IDENTITY: owner@example.com" in prompt
    assert "SENDER DISPLAY NAME: not verified; omit a name if unsure" in prompt
    assert "do not answer an older incoming request again" in prompt
    assert "Follow up about the schedule only" in prompt
    assert "do not reopen an earlier request" in ai.structured.call_args.kwargs["instructions"]
    assert "never write as if a past event is upcoming" in ai.structured.call_args.kwargs["instructions"]
    assert "never silently commit the account owner" in ai.structured.call_args.kwargs["instructions"]
    assert "never use placeholders" in ai.structured.call_args.kwargs["instructions"]


def test_email_handle_is_not_used_as_a_reply_signoff(repository):
    repository.db.execute(
        "UPDATE accounts SET email=?,display_name=? WHERE id='account-1'",
        ("owner@example.edu", "Owner"),
    )
    incoming_id = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="choice", provider_thread_id="choice",
            sender_email="karin@example.net", to=["owner@example.edu"],
            subject="Section choice", body_text="Would you like to switch sections?",
            sent_at=datetime.now(UTC),
        ),
    )
    thread_id = repository.db.scalar(
        "SELECT thread_id FROM messages WHERE id=?", (incoming_id,)
    )
    ai = Mock()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(
                label=label, stance="Reply",
                body=f"Hi Karin,\n\n{label} response.\n\nBest,\nOwner",
            )
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())

    result = service.draft_reply(thread_id, "Offer three alternatives")

    prompt = ai.structured.call_args.kwargs["input_text"]
    assert "SENDER DISPLAY NAME: not verified; omit a name if unsure" in prompt
    assert "SENDER DISPLAY NAME: Owner" not in prompt
    assert "never infer a name from the email address" in ai.structured.call_args.kwargs["instructions"]
    assert all(candidate.body == f"Hi Karin,\n\n{candidate.label} response." for candidate in result.candidates)


def test_sender_signoff_guard_keeps_a_demonstrated_full_name():
    result = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Reply", body=f"{label} reply.\n\nBest,\nTaylor")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    account = {"email": "owner@example.edu", "display_name": "Owner"}
    assert AssistantService._verified_sender_name(account) == ""
    cleaned = AssistantService._remove_unverified_signoffs(result, account)
    assert all(candidate.body.endswith("Best,\nTaylor") for candidate in cleaned.candidates)


def test_sender_signoff_guard_removes_orphaned_closing():
    result = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Reply", body=f"{label} reply.\n\nBest,")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    cleaned = AssistantService._remove_unverified_signoffs(
        result, {"email": "owner@example.edu", "display_name": "Owner"}
    )
    assert all(candidate.body == f"{candidate.label} reply." for candidate in cleaned.candidates)


def test_new_message_draft_does_not_sign_with_account_handle(repository):
    repository.db.execute(
        "UPDATE accounts SET email=?,display_name=? WHERE id='account-1'",
        ("owner@example.edu", "Owner"),
    )
    ai = Mock()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(
                label=label, stance="New message",
                body=f"Hello,\n\n{label} introduction.\n\nRegards,\nO.W.N.E.R.",
            )
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())

    result = service.draft_new(
        "account-1", ["karin@example.net"], "Introduction", "Introduce myself briefly",
    )

    assert "SENDER DISPLAY NAME: not verified" in ai.structured.call_args.kwargs["input_text"]
    assert all(
        candidate.body == f"Hello,\n\n{candidate.label} introduction."
        for candidate in result.candidates
    )


def test_old_incoming_thread_requires_user_goal_before_drafting(repository):
    message_time = datetime.now(UTC) - timedelta(days=60)
    identifier = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="expired-invitation",
            provider_thread_id="expired-invitation",
            sender_email="maya@example.net",
            body_text="Please join an optional event next month if you would like to attend.",
            sent_at=message_time,
        ),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (identifier,))
    ai = Mock()
    service = AssistantService(repository, Mock(), ai, AppSettings())
    with pytest.raises(ValueError, match="over 30 days old"):
        service.draft_reply(thread_id)
    ai.structured.assert_not_called()
    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Reply", body=f"{label}: Thank you.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    search = Mock()
    search.search.return_value = []
    historical = AssistantService(repository, search, ai, AppSettings())
    reference_time = message_time + timedelta(days=1)
    historical.draft_reply(thread_id, reference_now=reference_time)
    assert (
        "CURRENT LOCAL DATE: " + reference_time.astimezone().date().isoformat()
    ) in ai.structured.call_args.kwargs["input_text"]


def test_draft_references_keep_factual_sources_separate_from_sent_style_examples(repository):
    incoming_id = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="reference-source",
            provider_thread_id="reference-thread",
            sender_email="maya@example.net",
            body_text="The event will take place on Thursday afternoon.",
        ),
    )
    sent_id = sent(repository, "reference-style")
    result = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(
                label=label, stance="Reply", body="Thank you for the details.",
                supporting_source_ids=[incoming_id, sent_id],
                tone_example_ids=[sent_id, incoming_id],
            )
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    service = AssistantService(repository, Mock(), Mock(), AppSettings())
    verified = service._verify_draft_references(
        result, "account-1", [incoming_id], [sent_id, incoming_id]
    )
    for candidate in verified.candidates:
        assert candidate.supporting_source_ids == [incoming_id]
        assert candidate.tone_example_ids == [sent_id]
    assert verified.warnings


def test_identical_model_reply_bodies_are_not_presented_as_three_choices():
    result = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Reply", body="No reply needed.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    with pytest.raises(ValueError, match="three distinct options"):
        AssistantService._ensure_distinct_draft_choices(result)


def test_answer_prose_hides_raw_source_ids_but_preserves_ordinary_brackets():
    first = "0decd9e0-4a85-5804-9044-18492a527369"
    second = "8019009d-bacf-5c11-adf0-79e87d4070f7"
    raw = (
        f"Use your CNetID. [Sources: {first}, {second}]\n"
        f"Contact the office. [SOURCE:{first}] [Page 1]\n"
        f"Read the card. [{second}] \ue200cite\ue202{first}\ue201"
    )
    cleaned = AssistantService._clean_inline_source_ids(raw)
    assert cleaned == "Use your CNetID.\nContact the office. [Page 1]\nRead the card."


def test_unmonitored_notice_does_not_generate_reply_choices(repository):
    identifier = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="automated-alert",
            provider_thread_id="automated-alert",
            sender_email="forms-receipts-noreply@example.net",
            body_text="This is an automated security notice. Replies are not monitored.",
        ),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (identifier,))
    ai = Mock()
    with pytest.raises(ValueError, match="does not accept replies"):
        AssistantService(repository, Mock(), ai, AppSettings()).draft_reply(thread_id)
    ai.structured.assert_not_called()


def test_informational_notice_needs_explicit_reply_intent(repository):
    identifier = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="weekly-notification",
            provider_thread_id="weekly-notification",
            sender_email="notifications@example.net",
            body_text=("You received this email because you are participating in a class. "
                       "Visit notification settings to turn off email notifications. " * 2),
        ),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (identifier,))
    ai = Mock()
    search = Mock()
    search.search.return_value = []
    service = AssistantService(repository, search, ai, AppSettings())
    with pytest.raises(ValueError, match="informational notice"):
        service.draft_reply(thread_id)
    ai.structured.assert_not_called()

    ai.structured.return_value = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="Question", body=f"{label}: I have a question.")
            for label in ("Concise", "Warm", "Detailed")
        ],
    )
    result = service.draft_reply(thread_id, "Ask the course office which deadline applies")
    assert len(result.candidates) == 3


def test_signature_only_acknowledgment_needs_explicit_reply_intent():
    assert reply_requires_instruction(
        "Thank you,\n\nXiaoyu Yin, Agent\npronouns: She/Her\nhttps://example.com/agent"
    )
    assert not reply_requires_instruction(
        "Thanks, I will send the requested documents tomorrow. Please confirm receipt."
    )


def test_compact_reply_expands_to_normal_draft_fields_without_changing_default():
    compact = CompactReplySet(
        decision_detected=True,
        question="Which time?",
        drafts=[
            CompactReplyChoice(
                label=label,
                stance=label,
                assumption="If that time works",
                body=f"I can do {label}.",
                source_ids=["S1"],
                style_ids=["S2"],
            )
            for label in ("Morning", "Afternoon", "Ask")
        ],
        style_note="Brief and warm.",
    )
    expanded = compact.expand()
    assert expanded.decision_detected
    assert expanded.decision_question == "Which time?"
    assert expanded.candidates[0].supporting_source_ids == ["S1"]
    assert expanded.candidates[0].tone_example_ids == ["S2"]
    assert expanded.tone_guidance == "Brief and warm."
    assert not AppSettings().compact_reply_experiment


def test_tone_expiry_and_exact_person_isolation(repository):
    maya = sent(repository)
    sent(repository, "other", "other@example.net")
    assert [
        row["message_id"] for row in repository.get_tone_examples("account-1", "maya@example.net")
    ] == [maya]
    repository.save_tone_note("account-1", "maya@example.net", "Warm", [maya])
    assert repository.cached_tone_note("account-1", "other@example.net") is None
    assert repository.cached_tone_note("different-account", "maya@example.net") is None
    repository.db.execute(
        "UPDATE tone_notes SET updated_at=?",
        ((datetime.now(UTC) - timedelta(days=31)).isoformat(),),
    )
    assert repository.cached_tone_note("account-1", "maya@example.net") is None
    ai = Mock()
    ai.structured.return_value = ToneNote(guidance="Refreshed")
    service = AssistantService(repository, Mock(), ai, AppSettings())
    assert service.tone_note("account-1", "maya@example.net")["document"] == "Refreshed"
    assert ai.structured.call_count == 1


def test_unknown_recipients_share_account_fallback_without_claiming_relationship(repository):
    source = sent(repository)
    ai = Mock()
    ai.structured.return_value = ToneNote(guidance="Brief, friendly account-wide style.")
    service = AssistantService(repository, Mock(), ai, AppSettings())
    for recipient in ("new1@example.org", "new2@example.org", "new3@example.org"):
        note = service.tone_note("account-1", recipient)
        assert note["recipient"] == ""
        assert note["tone_example_ids"] == [source]
    assert ai.structured.call_count == 1
    assert service.tone_note("account-1", "maya@example.net")["recipient"] == "maya@example.net"
    assert ai.structured.call_count == 2


def test_tone_document_survives_encrypted_database_restart(tmp_path, monkeypatch):
    from email_reader.database import Database
    from email_reader.repository import MailRepository
    from email_reader.security import EncryptedBlobStore

    monkeypatch.delenv("EMAIL_READER_TESTING", raising=False)
    path = tmp_path / "encrypted.db"
    db = Database(path, "test-private-passphrase")
    db.open()
    blobs = EncryptedBlobStore(tmp_path / "blobs", b"k" * 32)
    repo = MailRepository(db, blobs)
    repo.upsert_account(Account(id="a", provider=ProviderKind.GMAIL, email="owner@example.com"))
    repo.save_tone_note("a", "person@example.net", "Private warm tone document", [])
    db.close()
    assert b"Private warm tone document" not in path.read_bytes()
    restarted = Database(path, "test-private-passphrase")
    restarted.open()
    assert (
        MailRepository(restarted, blobs).cached_tone_note("a", "person@example.net")["document"]
        == "Private warm tone document"
    )
    restarted.close()


def test_recipient_suggestions_names_email_dedup_account_and_sync(repository):
    sent(repository)
    sent(repository, "duplicate")
    assert repository.recipient_suggestions("account-1", "MAYA") == [
        {"email": "maya@example.net", "name": "Maya", "label": "Maya <maya@example.net>"}
    ]
    assert repository.recipient_suggestions("account-1", "owner") == []
    repository.upsert_account(
        Account(id="other-account", provider=ProviderKind.GMAIL, email="second@example.com")
    )
    assert repository.recipient_suggestions("other-account", "maya") == []
    sent(repository, "new", "New Contact <new@example.net>")
    assert (
        repository.recipient_suggestions("account-1", "New Contact")[0]["email"]
        == "new@example.net"
    )


@pytest.mark.parametrize(
    "original, expected",
    [
        ("New reply\n\nOn Tue, Alex wrote:\nOld reply", "New reply"),
        ("New reply\n\nOn Tue,\nAlex <alex@example.com>\nwrote:\nOld reply", "New reply"),
        ("> Question one\nAnswer one\n> Question two\nAnswer two", "Answer one\nAnswer two"),
        ("On Tue, Alex wrote:\n> Old reply\n\nBottom reply", "Bottom reply"),
        ("New\nFrom: Alex\nSent: yesterday\nTo: Me\nSubject: Re\nOld", "New"),
        ("From: New York\nTo: London\nTravel plans", "From: New York\nTo: London\nTravel plans"),
        ("New\n-----Original Message-----\nOld", "New"),
        (
            "Thanks, that works.\nBest,\nAlex\n获取 Outlook for iOS<https://aka.ms/example>\n"
            "________________________________\n发件人: Maya <maya@example.net>\n"
            "发送时间: Monday\n收件人: Alex\n主题: Re: Plans\nOld details",
            "Thanks, that works.\nBest,\nAlex",
        ),
        (
            "New reply\n________________________________\n发件人: Maya\n"
            "发送时间: Monday\n收件人: Alex\n主题: Re: Plans\nOld details",
            "New reply",
        ),
        (
            "New reply\n发件人: Maya\n发送时间: Monday\n收件人: Alex\n"
            "主题: Re: Plans\nOld details",
            "New reply",
        ),
        ("The label says 发件人: Maya, but the item is mine.",
         "The label says 发件人: Maya, but the item is mine."),
    ],
)
def test_quote_display_preserves_new_inline_and_bottom_replies(original, expected):
    assert message_new_content(original) == expected


def test_html_quoted_history_and_multiline_outlook_headers():
    original = "New reply\nPrevious conversation without plain-text markers"
    html = '<div>New reply</div><div class="gmail_quote"><div>Previous conversation without plain-text markers</div></div>'
    assert message_new_content(original, html) == "New reply"
    assert (
        message_new_content("New\nFrom:\nAlex\nSent:\nyesterday\nTo:\nMe\nSubject:\nRe\nOld")
        == "New"
    )


def test_newest_first_display_full_original_and_reply_target(repository):
    old = MessagePayload(
        provider_id="old",
        provider_thread_id="conversation",
        sender_email="old@example.net",
        body_text="Old",
        sent_at=datetime.now(UTC) - timedelta(days=2),
    )
    new = MessagePayload(
        provider_id="new",
        provider_thread_id="conversation",
        sender_email="latest@example.net",
        body_text="New\nOn Tue, Alex wrote:\nOld",
        sent_at=datetime.now(UTC),
    )
    repository.ingest_message("account-1", old)
    repository.ingest_message("account-1", new)
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(repository=repository, database=repository.db, assistant=Mock())
    from email_reader.mail_images import MailImageProvider

    controller.mail_images = MailImageProvider(repository.blobs)
    controller._threads = repository.list_threads()
    controller._accounts = repository.list_accounts()
    controller._selected_account = ""
    controller._selected_mailbox = "inbox"
    controller._selected_category = "primary"
    controller.selectThread(repository.list_threads()[0]["id"])
    messages = controller.selectedThread["messages"]
    assert [item["provider_id"] for item in messages] == ["new", "old"]
    assert messages[0]["new_content"] == "New"
    assert messages[0]["body_text"] == new.body_text
    assert messages[0]["has_quoted_history"]
    assert not messages[1]["has_quoted_history"]
    # Review the latest reply in the one supported editor. No send operation exists.
    provider = Mock()
    provider.create_draft.side_effect = RuntimeError("stop after capturing request")
    controller._provider = lambda _: provider
    controller._editor_draft = {}
    controller._busy = False
    controller._set_status = Mock()
    controller.beginDraft("reply")
    controller._run = lambda label, work, done, **kw: work(lambda _: None)
    with pytest.raises(RuntimeError, match="capturing"):
        controller.saveEditor({**controller.editorDraft, "body": "Thanks!"})
    request = provider.create_draft.call_args.args[0]
    assert request.to == ["latest@example.net"]
    assert request.reply_to_provider_message_id == "new"


def test_editor_ignores_stale_reply_suggestions(repository):
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(repository=repository)
    controller._accounts = repository.list_accounts()
    controller._selected_thread = {}
    controller._editor_draft = initial_draft("new", {}, controller._accounts, "account-1")
    controller._editor_candidates = []
    controller._busy = False
    controller._set_status = Mock()
    callbacks = []
    controller._run = lambda label, work, done, **kw: callbacks.append(done)
    choices = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(label=label, stance="accept", body="Yes")
            for label in ["Concise", "Warm", "Detailed"]
        ],
    )
    form = {**controller.editorDraft, "to": "friend@example.net", "subject": "Hello"}
    controller.assistEditor(form, "Say hello")
    controller.assistEditor(form, "Say hello again")
    callbacks[0](choices)
    assert controller.editorCandidates == []
    callbacks[1](choices)
    assert len(controller.editorCandidates) == 3
    assert controller.editorDraft["body"] == ""
    controller.discardEditor()
    callbacks[1](choices)
    assert controller.editorCandidates == []
