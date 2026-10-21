"""Fixed real-mail relevance checks. Private evidence is saved only in SQLCipher.

These are coverage checks, not an automatic factual/style quality verdict. The
questions and expected sources were selected from inspected mail before running
the suite. Never tailor the cases to a generated answer or overwrite a failed run.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(frozen=True)
class SearchCase:
    question: str
    subject: str
    scope: str = "all"
    follow_up: int | None = None
    required_files: tuple[str, ...] = ()
    # Every group must match; alternatives within a group are ORed.
    terms: tuple[str, ...] = ()


CASES = (
    SearchCase("auto insurance", "STATEFARM DOCUMENTS",
               required_files=("2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf")),
    SearchCase("Find the insurance ID card for my 2022 BMW.", "STATEFARM DOCUMENTS",
               required_files=("2022 BMW ID CARD.pdf",)),
    SearchCase("What do I need to do for the State Farm defensive-driving discount?",
               "STATEFARM DOCUMENTS", terms=(r"three|3|三", r"14|fourteen|十四", r"1\s*(?:-|–)?\s*(?:hr|hour)|one.hour|一小时")),
    SearchCase("What coupon code and course price did they give me?", "STATEFARM DOCUMENTS",
               follow_up=2, terms=(r"GAP5R5", r"13\.95")),
    SearchCase("What did the State Farm agent ask me to provide for watch insurance?",
               "STATEFARM DOCUMENTS", terms=(r"photo|picture|照片",)),
    SearchCase("Find the email about my 727 monthly parking agreement and who will contact me about payment.",
               "727 Monthly Parking Agreement", terms=(r"Legacy Parking",)),
    SearchCase("How do I access the PSD 2026 online orientation course?",
               "PSD 2026 New Student Orientation Online Course", scope="work_gmail",
               terms=(r"CNet", r"Canvas")),
    SearchCase("When and where is the PSD in-person orientation, and is attendance required?",
               "PSD 2026 New Student Orientation Online Course", scope="outlook",
               terms=(r"September\s+23|Sept?\.?\s+23|23\s+September|9月23", r"Logan",
                      r"not required|optional|not mandatory|encouraged.*not|非强制|不.*必须")),
    SearchCase("Give me the link to the 2026 MPCS orientation schedule.",
               "Orientation Begins Next Week!", terms=(r"https://canvas\.uchicago\.edu/courses/70202/files/15759374",)),
    SearchCase("Which two buildings will MPCS orientation take place in?",
               "Orientation Begins Next Week!", terms=(r"Kent", r"Crerar|JCL")),
)


def prepare_cases(database) -> list[dict]:
    """Resolve known-source scopes before any model call; never guess an account."""
    accounts = database.query("SELECT id,provider FROM accounts WHERE enabled=1")
    if len(accounts) != 3:
        raise ValueError("The acceptance suite requires three enabled accounts")
    insurance = database.query(
        "SELECT DISTINCT account_id FROM threads WHERE subject='STATEFARM DOCUMENTS' AND trashed_at IS NULL"
    )
    personal = {row["account_id"] for row in insurance}
    scopes = {
        "work_gmail": [row["id"] for row in accounts if row["provider"] == "gmail" and row["id"] not in personal],
        "outlook": [row["id"] for row in accounts if row["provider"] == "outlook"],
    }
    if any(len(values) != 1 for values in scopes.values()):
        raise ValueError("Expected account scopes are ambiguous or unavailable")
    prepared = []
    for case in CASES:
        account_id = None if case.scope == "all" else scopes[case.scope][0]
        rows = database.query(
            "SELECT id FROM threads WHERE instr(lower(subject),lower(?))>0 AND trashed_at IS NULL "
            "AND (? IS NULL OR account_id=?)",
            (case.subject, account_id, account_id),
        )
        if not rows:
            raise ValueError("A preselected ground-truth source is unavailable")
        prepared.append({"case": asdict(case), "account_id": account_id,
                         "expected_thread_ids": [row["id"] for row in rows]})
    return prepared


def audit_local_retrieval(context) -> dict:
    """Two fixed retrieval probes; no AI/embedding calls or private text output.

    This checks rankings with supplied translations, not query planning or
    answer accuracy, and never substitutes for the ten-question acceptance run.
    """
    from email_reader.search import HybridSearch

    search = HybridSearch(context.database, context.repository, embedder=None)
    probes = (
        (["auto insurance", "汽车保险", "保险卡"],
         "STATEFARM DOCUMENTS", ("2022 BMW BINDER.pdf", "2022 BMW ID CARD.pdf")),
        (["What did the State Farm agent ask me to provide for watch insurance?",
          "State Farm insurance", "State Farm", "insurance payment", "payment receipt",
          "personal articles insurance", "手表保险"], "STATEFARM DOCUMENTS", ()),
    )
    report = []
    for index, (queries, subject, filenames) in enumerate(probes, 1):
        hits = search.conversational_search(queries)
        expected_threads = {row["id"] for row in context.database.query(
            "SELECT id FROM threads WHERE subject=? AND trashed_at IS NULL", (subject,)
        )}
        ranks = [rank for rank, hit in enumerate(hits, 1)
                 if context.database.scalar("SELECT thread_id FROM source_chunks WHERE id=?",
                                            (hit.chunk_id,)) in expected_threads]
        files = list(dict.fromkeys(hit.citation.title for hit in hits
                                  if hit.citation.source_type == "attachment"))
        report.append({"probe": index, "expected_source_ranks": ranks,
                       "required_files_in_top_three": all(name in files[:3] for name in filenames),
                       "retrieved_count": len(hits)})
    return {"local_only": True, "supplied_translations": True,
            "answer_quality_evaluated": False, "probes": report}


def check_answer(database, prepared: dict, answer, hits, *, blobs=None, file_cards=None) -> dict:
    hit_ids = {hit.chunk_id for hit in hits}
    cited = list(dict.fromkeys(answer.source_ids))
    records = [database.query("SELECT account_id,thread_id FROM source_chunks WHERE id=?", (identifier,))
               for identifier in cited]
    resolved = bool(cited) and set(cited).issubset(hit_ids) and all(records)
    expected = set(prepared["expected_thread_ids"])
    correct_source = any(rows and rows[0]["thread_id"] in expected for rows in records)
    account_id = prepared["account_id"]
    scoped = account_id is None or all(
        database.scalar("SELECT account_id FROM source_chunks WHERE id=?", (identifier,)) == account_id
        for identifier in hit_ids
    )
    files = []
    displayed_ids = (
        [card["id"] for card in file_cards[:3]]
        if file_cards is not None else answer.attachment_ids[:3]
    )
    for identifier in displayed_ids:
        rows = database.query(
            "SELECT a.filename,a.content_digest,m.sent_at,m.thread_id FROM attachments a "
            "JOIN messages m ON m.id=a.message_id WHERE a.id=?", (identifier,)
        )
        for row in rows:
            item = dict(row)
            item["original_verified"] = False
            if blobs is not None and row["content_digest"]:
                try:
                    blobs.get(row["content_digest"])  # Authenticated decryption, memory only.
                    item["original_verified"] = True
                except Exception:
                    pass
            files.append(item)
    case = prepared["case"]
    file_matches = all(any(row["filename"] == name and row["original_verified"] and row["sent_at"]
                           and row["thread_id"] in expected for row in files)
                       for name in case["required_files"])
    matched_terms = [bool(re.search(pattern, answer.text, re.I)) for pattern in case["terms"]]
    checks = {"citations_resolve": resolved, "expected_source_cited": correct_source,
              "account_scope_correct": scoped, "required_originals_in_top_three": file_matches,
              "required_detail_coverage": all(matched_terms),
              "sufficient_evidence": not answer.insufficient_evidence}
    return {"checks": checks, "coverage_pass": all(checks.values()), "term_matches": matched_terms}


def audit_search_suite(context) -> dict:
    prepared = prepare_cases(context.database)
    key = "search_suite_v1_" + uuid4().hex
    record = {"version": 1, "started_at": datetime.now(UTC).isoformat(), "status": "running",
              "cases": prepared, "runs": [], "manual_quality_review": "pending"}

    def save():
        context.database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(record)),
        )

    save()  # Freeze the complete test manifest before generating any answer.
    for index, item in enumerate(prepared):
        case = item["case"]
        history = []
        if case["follow_up"] is not None:
            previous = record["runs"][case["follow_up"]]
            history = [{"role": "user", "text": prepared[case["follow_up"]]["case"]["question"]},
                       {"role": "assistant", "text": previous["answer"]["text"]}]
        started = time.perf_counter()
        try:
            previous_capture = getattr(context.assistant, "capture_search_diagnostics", False)
            context.assistant.capture_search_diagnostics = True
            try:
                answer, hits = context.assistant.answer(case["question"], account_id=item["account_id"], history=history)
                private_diagnostics = getattr(context.assistant, "last_search_diagnostics", {})
                if not isinstance(private_diagnostics, dict):
                    private_diagnostics = {}
                # Freeze independent state; later calls must not change a
                # completed case. Never print the plan or add it to reports.
                private_diagnostics = json.loads(json.dumps(private_diagnostics))
            finally:
                context.assistant.capture_search_diagnostics = previous_capture
            file_cards = context.repository.answer_file_cards(
                answer.attachment_ids, answer.related_attachment_ids, question=case["question"]
            )
            checks = check_answer(context.database, item, answer, hits,
                                  blobs=context.repository.blobs, file_cards=file_cards)
        except Exception as exc:
            # Stop on API/budget errors. Never retry around the real usage cap.
            record["status"] = "stopped"
            record["stopped_case"] = index + 1
            record["error_type"] = type(exc).__name__
            save()
            raise
        elapsed = round(time.perf_counter() - started, 2)
        record["runs"].append({"answer": answer.model_dump(mode="json"),
                               "private_diagnostics": private_diagnostics,
                               "file_card_ids": [card["id"] for card in file_cards],
                               "sources": [hit.citation.model_dump(mode="json") for hit in hits],
                               "seconds": elapsed, **checks})
        save()
        print(json.dumps({"case": index + 1, "seconds": elapsed, **checks}), flush=True)
    record["status"] = "finished"
    save()
    return {"completed": len(record["runs"]),
            "coverage_passes": sum(run["coverage_pass"] for run in record["runs"]),
            "manual_quality_review": "pending", "private_results_saved_encrypted": True}


def search_suite_report(context) -> dict:
    """Inspect failure location without emitting answers, prompts, IDs or excerpts."""
    value = context.database.scalar(
        "SELECT value FROM app_settings WHERE key LIKE 'search_suite_v1_%' ORDER BY rowid DESC LIMIT 1"
    )
    if not value:
        raise ValueError("No recorded search suite exists")
    record = json.loads(value)
    report = {"status": record["status"], "cases": []}
    for index, run in enumerate(record["runs"]):
        prepared = record["cases"][index]
        expected = set(prepared["expected_thread_ids"])
        source_ranks = [rank for rank, source in enumerate(run["sources"], 1)
                        if context.database.scalar("SELECT thread_id FROM source_chunks WHERE id=?",
                                                   (source["source_id"],)) in expected]
        files = []
        selected = run["answer"]["attachment_ids"]
        displayed = run.get("file_card_ids", selected)
        for name in prepared["case"]["required_files"]:
            ids = {row["id"] for row in context.database.query("SELECT id FROM attachments WHERE filename=?", (name,))}
            files.append({"filename": name,
                          "cited_ranks": [rank for rank, source in enumerate(run["sources"], 1)
                                          if source["source_type"] == "attachment" and source["title"] == name
                                          and source["source_id"] in run["answer"]["source_ids"]],
                          "selected_ranks": [rank for rank, identifier in enumerate(selected, 1) if identifier in ids],
                          "displayed_ranks": [rank for rank, identifier in enumerate(displayed, 1) if identifier in ids],
                          "retrieved_ranks": [rank for rank, source in enumerate(run["sources"], 1)
                                              if source["source_type"] == "attachment" and source["title"] == name]})
        report["cases"].append({"case": index + 1, "coverage_pass": run["coverage_pass"],
                                "failed_checks": [name for name, passed in run["checks"].items() if not passed],
                                "term_matches": run["term_matches"],
                                "expected_source_retrieved_ranks": source_ranks, "required_files": files})
    return report


def search_case_review_text(context, case_number: int) -> str:
    """Prepare one encrypted evaluation case for an in-memory, read-only window."""
    if not 1 <= case_number <= len(CASES):
        raise ValueError("Choose one of the ten search cases")
    value = context.database.scalar(
        "SELECT value FROM app_settings WHERE key LIKE 'search_suite_v1_%' "
        "ORDER BY rowid DESC LIMIT 1"
    )
    if not value:
        raise ValueError("No recorded search suite exists")
    record = json.loads(value)
    index = case_number - 1
    if index >= len(record.get("runs", [])):
        raise ValueError("This case has not completed")
    prepared = record["cases"][index]
    run = record["runs"][index]
    answer = run["answer"]
    lines = [
        f"CASE {case_number}: {prepared['case']['question']}",
        f"Automated coverage: {'pass' if run['coverage_pass'] else 'incomplete'}",
        f"Model marked evidence incomplete: {answer['insufficient_evidence']}",
        "",
        "ANSWER",
        answer["text"],
        "",
    ]
    diagnostics = run.get("private_diagnostics", {})
    if diagnostics:
        lines.extend([
            "SEARCH DIAGNOSTICS (generated query plan, not source evidence)",
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            "",
        ])
    cards = context.repository.answer_file_cards(
        answer["attachment_ids"], answer.get("related_attachment_ids", []),
        question=prepared["case"]["question"],
    )
    if cards:
        lines.append("CURRENT LOCAL FILE DATE HINTS (not part of the saved answer or score)")
        for card in cards:
            lines.append(card["filename"])
            for duplicate in card["duplicate_sources"]:
                lines.append("Identical original also attached as: " + duplicate["filename"])
            if not card["document_dates"]:
                lines.append("No unambiguous labelled date recognized")
            for date in card["document_dates"]:
                lines.extend([
                    f"{date['label']}: {date['value']}  Location: {date['locator']}",
                    f"Source: {date['source_id']}",
                    f"Exact extracted span: {date['source_span']}",
                ])
        lines.append("")
    lines.append("CITED SOURCES (complete stored chunks, not necessarily complete originals)")
    by_id = {source["source_id"]: source for source in run["sources"]}
    for identifier in answer["source_ids"]:
        source = by_id.get(identifier)
        if not source:
            lines.append("Citation unavailable in the recorded retrieval set")
            continue
        chunk = context.database.scalar(
            "SELECT content FROM source_chunks WHERE id=?", (identifier,)
        )
        lines.extend([
            "",
            f"{source['source_type'].upper()}: {source['title']}",
            f"Citation ID: {identifier}",
            f"From: {source['sender']}  Date: {source['sent_at']}  "
            f"Account: {source['account_email']}  Location: {source['locator']}",
            str(chunk) if chunk else "Stored citation content unavailable; verify the original source.",
        ])
    if not answer["source_ids"]:
        lines.append("No citations")
    return "\n".join(lines)
