import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from email_reader.contracts import Account, Answer, AttachmentPayload, MessagePayload, ProviderKind
from email_reader.search import SearchHit
from email_reader.search_audit import (
    CASES,
    audit_local_retrieval,
    audit_search_suite,
    check_answer,
    prepare_cases,
    search_case_review_text,
    search_suite_report,
)


def test_local_retrieval_probe_is_offline_and_outputs_no_mail(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="watch", provider_thread_id="watch", subject="STATEFARM DOCUMENTS",
        body_text="State Farm: 手表保险，麻烦提供手表照片。 private source marker",
    ))
    unused_service = Mock()
    context = SimpleNamespace(database=repository.db, repository=repository,
                              assistant=unused_service, search=unused_service)
    report = audit_local_retrieval(context)
    assert report["local_only"] and report["supplied_translations"]
    assert not report["answer_quality_evaluated"]
    assert report["probes"][1]["expected_source_ranks"] == [1]
    assert not report["probes"][0]["required_files_in_top_three"]
    assert not unused_service.mock_calls
    output = json.dumps(report)
    assert "private" not in output and "State Farm" not in output and "手表" not in output


def seed_cases(repository):
    for identifier, provider in (("work", ProviderKind.GMAIL), ("school", ProviderKind.OUTLOOK)):
        repository.upsert_account(Account(id=identifier, provider=provider, email=f"{identifier}@example.com"))
    seeded = set()
    for index, case in enumerate(CASES):
        account = {"all": "account-1", "work_gmail": "work", "outlook": "school"}[case.scope]
        if (account, case.subject) in seeded:
            continue
        seeded.add((account, case.subject))
        repository.ingest_message(account, MessagePayload(provider_id=str(index), provider_thread_id=str(index),
            subject=case.subject, body_text="private evaluation source", labels=["INBOX"]))


def hit_for(repository, thread_id):
    row = repository.db.query("SELECT * FROM source_chunks WHERE thread_id=? LIMIT 1", (thread_id,))[0]
    return SearchHit(row["id"], row["content"], 1, repository.citation_for_chunk(row["id"]))


def test_fixed_suite_resolves_three_accounts_before_any_model_call(repository):
    with pytest.raises(ValueError):
        prepare_cases(repository.db)
    seed_cases(repository)
    prepared = prepare_cases(repository.db)
    assert len(prepared) == 10
    assert prepared[6]["account_id"] == "work"
    assert prepared[7]["account_id"] == "school"
    assert prepared[3]["case"]["follow_up"] == 2
    repository.db.execute("UPDATE accounts SET enabled=0 WHERE id='school'")
    with pytest.raises(ValueError):
        prepare_cases(repository.db)


def test_relevance_checks_require_expected_cited_source_not_just_retrieved(repository):
    seed_cases(repository)
    prepared = prepare_cases(repository.db)
    correct = hit_for(repository, prepared[6]["expected_thread_ids"][0])
    wrong = hit_for(repository, prepared[7]["expected_thread_ids"][0])
    answer = Answer(text="Use CNet to access Canvas.", source_ids=[wrong.chunk_id])
    result = check_answer(repository.db, prepared[6], answer, [correct, wrong])
    assert not result["coverage_pass"]
    assert not result["checks"]["account_scope_correct"]
    assert not result["checks"]["expected_source_cited"]
    answer.source_ids = [correct.chunk_id]
    assert check_answer(repository.db, prepared[6], answer, [correct])["coverage_pass"]
    answer.text = "There is a course."
    assert not check_answer(repository.db, prepared[6], answer, [correct])["coverage_pass"]
    answer.text = "Use CNet to access Canvas."
    answer.insufficient_evidence = True
    assert not check_answer(repository.db, prepared[6], answer, [correct])["coverage_pass"]


def test_private_search_review_uses_saved_answer_and_cited_chunk(repository):
    seed_cases(repository)
    prepared = prepare_cases(repository.db)
    hit = hit_for(repository, prepared[0]["expected_thread_ids"][0])
    record = {
        "cases": prepared,
        "runs": [{
            "answer": Answer(
                text="A private test answer.", source_ids=[hit.chunk_id]
            ).model_dump(mode="json"),
            "sources": [hit.citation.model_dump(mode="json")],
            "coverage_pass": True,
        }],
    }
    repository.db.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?)",
        ("search_suite_v1_reviewtest", json.dumps(record)),
    )
    context = SimpleNamespace(database=repository.db, repository=repository)
    full_chunk = "private evaluation source " + "x" * 4000 + " END OF CITED EVIDENCE"
    repository.db.execute("UPDATE source_chunks SET content=? WHERE id=?", (full_chunk, hit.chunk_id))
    review = search_case_review_text(context, 1)
    assert "A private test answer." in review
    assert "private evaluation source" in review
    assert "CITED SOURCES" in review
    assert full_chunk in review
    assert f"Citation ID: {hit.chunk_id}" in review
    assert "not necessarily complete originals" in review
    repository.db.execute("UPDATE source_chunks SET content='' WHERE id=?", (hit.chunk_id,))
    assert "Stored citation content unavailable" in search_case_review_text(context, 1)
    with pytest.raises(ValueError, match="has not completed"):
        search_case_review_text(context, 2)


def test_audit_freezes_manifest_preserves_failure_and_never_logs_private_answers(repository, capsys):
    seed_cases(repository)
    prepared = prepare_cases(repository.db)
    index = 0

    def generate(question, account_id, history):
        nonlocal index
        assert repository.db.scalar("SELECT count(*) FROM app_settings WHERE key LIKE 'search_suite_v1_%'") >= 1
        assert context.assistant.capture_search_diagnostics is True
        context.assistant.last_search_diagnostics = {
            "query_plan": {"search_phrases": ["private planned phrase", "私密计划"]},
            "correction_attempted": False,
        }
        if index == 3:
            assert history[1]["text"] == "private answer marker"
            raise RuntimeError("private error details")
        hit = hit_for(repository, prepared[index]["expected_thread_ids"][0])
        index += 1
        return Answer(text="private answer marker", source_ids=[hit.chunk_id]), [hit]

    context = SimpleNamespace(database=repository.db, repository=repository, assistant=Mock())
    context.assistant.capture_search_diagnostics = False
    context.assistant.answer.side_effect = generate
    for _ in range(2):
        index = 0
        with pytest.raises(RuntimeError):
            audit_search_suite(context)
        assert context.assistant.capture_search_diagnostics is False
    records = repository.db.query("SELECT value FROM app_settings WHERE key LIKE 'search_suite_v1_%'")
    assert len(records) == 2  # No overwritten evidence or retries around the cap.
    for row in records:
        record = json.loads(row["value"])
        assert len(record["cases"]) == 10 and len(record["runs"]) == 3
        assert record["status"] == "stopped" and record["stopped_case"] == 4
        assert record["error_type"] == "RuntimeError"
        assert "private error details" not in row["value"]
        assert record["manual_quality_review"] == "pending"
        assert record["runs"][0]["private_diagnostics"]["query_plan"]["search_phrases"] == [
            "private planned phrase", "私密计划",
        ]
    output = capsys.readouterr().out
    assert "private" not in output and "STATEFARM" not in output
    assert '"case": 3' in output
    report = search_suite_report(context)
    assert len(report["cases"]) == 3
    assert report["cases"][0]["expected_source_retrieved_ranks"] == [1]
    assert report["cases"][0]["failed_checks"] == ["required_originals_in_top_three"]
    assert report["cases"][0]["term_matches"] == []
    assert "private" not in json.dumps(report)
    review = search_case_review_text(context, 1)
    assert "private planned phrase" in review and "私密计划" in review
    assert "not source evidence" in review


def test_file_acceptance_requires_available_original_and_top_three(repository):
    repository.ingest_message("account-1", MessagePayload(provider_id="files", provider_thread_id="files",
        subject="STATEFARM DOCUMENTS", body_text="The documents are attached.",
        attachments=[AttachmentPayload(provider_id=str(index), filename=name,
            mime_type="application/pdf" if index < 2 else "text/plain",
            content=None if index == 1 else b"original bytes") for index, name in enumerate(
                ["2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf", "extra.txt", "extra2.txt"])]))
    thread = repository.db.scalar("SELECT id FROM threads WHERE subject='STATEFARM DOCUMENTS'")
    hit = hit_for(repository, thread)
    files = [row["id"] for row in repository.db.query("SELECT id FROM attachments ORDER BY provider_id")]
    prepared = {"account_id": None, "expected_thread_ids": [thread], "case": {
        "required_files": ["2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf"], "terms": []}}
    answer = Answer(text="Files found.", source_ids=[hit.chunk_id], attachment_ids=files)
    assert not check_answer(repository.db, prepared, answer, [hit], blobs=repository.blobs)["coverage_pass"]
    digest, _ = repository.blobs.put(b"recovered original")
    repository.db.execute("UPDATE attachments SET content_digest=? WHERE id=?", (digest, files[1]))
    assert check_answer(repository.db, prepared, answer, [hit], blobs=repository.blobs)["coverage_pass"]
    answer.attachment_ids = [files[0], files[2], files[3], files[1]]
    assert not check_answer(repository.db, prepared, answer, [hit], blobs=repository.blobs)["coverage_pass"]
    cards = repository.answer_file_cards(answer.attachment_ids, [])
    assert [card["filename"] for card in cards[:2]] == [
        "2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf",
    ]
    assert check_answer(repository.db, prepared, answer, [hit],
                        blobs=repository.blobs, file_cards=cards)["coverage_pass"]
