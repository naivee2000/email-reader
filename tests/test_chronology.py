from unittest.mock import Mock

import pytest

from email_reader.ai import AssistantService, QueryPlan
from email_reader.chronology import message_order_errors
from email_reader.config import AppSettings
from email_reader.contracts import Answer, Citation, MessageOrderClaim
from email_reader.search import SearchHit


def hits(first="2026-09-10T20:00:00-05:00", second="2026-09-11T00:30:00+00:00"):
    return [SearchHit(identifier, "Supported message content", 1, Citation(
        source_id=identifier, source_type="message", title="Message", sent_at=date,
        account_email="owner@example.com", app_link=f"emailreader://message/{identifier}",
    )) for identifier, date in [("a", first), ("b", second)]]


def ordered(text="A later email supplies a contact.", relation="later"):
    return Answer(text=text, source_ids=["a", "b"], message_order_claims=[
        MessageOrderClaim(span=text, source_id="a", reference_source_id="b", relation=relation),
    ])


def test_ordering_uses_timezone_offsets_not_local_date_or_retrieval_order():
    assert not message_order_errors(ordered(), hits())
    assert message_order_errors(ordered(), hits("2026-09-10T12:00:00Z", "2026-09-13T12:00:00Z"))


def test_relation_must_agree_with_visible_text():
    assert message_order_errors(ordered(relation="earlier"),
                                hits("2026-09-10T12:00:00Z", "2026-09-13T12:00:00Z"))


@pytest.mark.parametrize("date", ["", "not a date", "2026-09-10T12:00:00"])
def test_missing_or_naive_dates_cannot_verify_order(date):
    assert message_order_errors(ordered(), hits(date))


def test_unknown_sources_uncited_sources_missing_span_and_equal_times_rejected():
    answer = ordered()
    answer.message_order_claims[0].source_id = "unknown"
    assert message_order_errors(answer, hits())
    answer = ordered()
    answer.source_ids = ["a"]
    assert message_order_errors(answer, hits())
    answer = ordered()
    answer.message_order_claims[0].span = "Not present"
    assert message_order_errors(answer, hits())
    assert message_order_errors(ordered(), hits("2026-09-10T12:00:00Z", "2026-09-10T12:00:00Z"))


def test_unmapped_ordering_and_superlatives_rejected_but_event_time_not_checked():
    assert message_order_errors(Answer(text="A later email supplies a contact."), hits())
    assert message_order_errors(ordered(text="The latest email supplies a contact."), hits())
    assert message_order_errors(Answer(text="后续邮件提供联系方式。"), hits())
    assert not message_order_errors(Answer(text="The meeting was scheduled for later today."), hits())
    assert Answer.model_validate({"text": "Old cached answer"}).message_order_claims == []


def test_document_dates_cannot_substitute_for_message_timestamps():
    sources = hits()
    sources[0].citation.source_type = "attachment"
    assert message_order_errors(ordered(), sources)


def test_correction_cancellation_is_not_swallowed_or_retried(repository):
    search = Mock()
    search.conversational_search.return_value = hits("2026-09-10T12:00:00Z", "2026-09-13T12:00:00Z")
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Contact?", search_phrases=["contact"], response_type="information"),
        ordered(), RuntimeError("Operation cancelled"),
    ]
    with pytest.raises(RuntimeError, match="Operation cancelled"):
        AssistantService(repository, search, ai, AppSettings()).answer("Contact?")
    assert ai.structured.call_count == 3


@pytest.mark.parametrize("corrected", [True, False])
def test_service_only_retries_once_and_keeps_failed_sources_as_possible_matches(repository, corrected):
    sources = hits("2026-09-10T12:00:00Z", "2026-09-13T12:00:00Z")
    search = Mock()
    search.conversational_search.return_value = sources
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Who is the contact?", search_phrases=["contact"], response_type="information"),
        ordered(),
        Answer(text="The September 10 email supplies a contact.", source_ids=["a"]) if corrected else ordered(),
    ]
    service = AssistantService(repository, search, ai, AppSettings())
    service.capture_search_diagnostics = True
    answer, _ = service.answer("Who is the contact?")
    assert service.last_search_diagnostics["query_plan"]["search_phrases"] == ["contact"]
    assert service.last_search_diagnostics["correction_attempted"]
    assert service.last_search_diagnostics["initial_ordering_error_count"] >= 1
    assert bool(service.last_search_diagnostics["remaining_ordering_error_count"]) is not corrected
    assert [call.kwargs["operation"] for call in ai.structured.call_args_list] == [
        "search_plan", "answer", "answer_correction",
    ]
    if corrected:
        assert answer.text == "The September 10 email supplies a contact."
        assert answer.source_ids == ["a"]
    else:
        assert answer.insufficient_evidence
        assert not answer.source_ids
        assert answer.related_source_ids == ["a", "b"]


def test_service_does_not_regenerate_answer_without_ordering_claims(repository):
    search = Mock()
    search.conversational_search.return_value = hits()
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Contact?", search_phrases=["contact"], response_type="information"),
        Answer(text="Here is the contact.", source_ids=["a"]),
    ]
    service = AssistantService(repository, search, ai, AppSettings())
    service.last_search_diagnostics = {"old": "private prior plan"}
    answer, _ = service.answer("Contact?")
    assert service.last_search_diagnostics == {}
    assert answer.text == "Here is the contact."
    assert ai.structured.call_count == 2
