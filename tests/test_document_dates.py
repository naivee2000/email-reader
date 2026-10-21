from __future__ import annotations

import pytest

from email_reader.contracts import AttachmentPayload, MessagePayload
from email_reader.document_dates import printed_document_dates


def chunk(text, identifier="chunk-1", locator="Page 1"):
    return {"id": identifier, "content": text, "locator": locator}


@pytest.mark.parametrize("text,label,value", [
    ("Effective Date\nSeptember 12, 2026\nExpiration Date\nMarch 12, 2027",
     "Effective date", "September 12, 2026"),
    ("Expiry date: 2027-03-12", "Expiration date", "2027-03-12"),
    ("生效日期：2026年9月12日", "Effective date", "2026年9月12日"),
    ("Issue date: 09/12/2026", "Issue date", "09/12/2026"),
    ("09-12-2026\nEffective date: , 09-11-2026/01:38 PM CST\nApplication date/time:",
     "Effective date", "09-12-2026"),
])
def test_explicit_printed_dates_keep_exact_source(text, label, value):
    dates = printed_document_dates([chunk(text)])
    date = next(date for date in dates if date["label"] == label)
    assert date["value"] == value
    assert date["source_span"] in text
    assert value in date["source_span"]
    assert date["source_id"] == "chunk-1"
    assert date["app_link"] == "emailreader://source/chunk-1"
    assert date["locator"] == "Page 1"


@pytest.mark.parametrize("text", [
    "Email sent September 12, 2026. Insurance 2022 BMW.pdf",
    "Not an effective date: September 12, 2026",
    "Effective date: unknown. September 12, 2026 is only an estimate.",
    "Effective Date\n[Page 2]\nSeptember 12, 2026",
    "Effective date: February 30, 2026",
    "Expiry date: 99/99/2027",
    "2026-09-12\nEffective Date\nunspecified",
    "Effective date: , 09-11-2026/01:38 PM CST\nApplication date/time:",
])
def test_unlabelled_ambiguous_or_invalid_dates_are_not_inferred(text):
    assert printed_document_dates([chunk(text)]) == []


def test_conflicting_values_withhold_only_conflicting_field():
    dates = printed_document_dates([
        chunk("Effective Date: September 12, 2026\nExpiration Date: March 12, 2027"),
        chunk("Effective Date: September 13, 2026", "chunk-2", "Page 2"),
    ])
    assert [(d["label"], d["value"]) for d in dates] == [
        ("Expiration date", "March 12, 2027")
    ]


def test_repeated_printed_dates_are_deduplicated_without_cross_chunk_inference():
    dates = printed_document_dates([
        chunk("Effective Date\nSeptember 12, 2026"),
        chunk("Effective Date: September 12 2026", "chunk-2"),
        chunk("Expiry Date", "chunk-3"),
        chunk("March 12, 2027", "chunk-4"),
    ])
    assert len(dates) == 1
    assert dates[0]["source_id"] == "chunk-1"


def test_reverse_pdf_columns_do_not_shift_expiration_into_effective_date():
    text = "Policy Number\nNovember 4, 2025\nEffective Date\nMay 4, 2026\nExpiration Date\nInsured"
    dates = printed_document_dates([chunk(text)])
    assert [(d["label"], d["value"]) for d in dates] == [
        ("Effective date", "November 4, 2025"),
        ("Expiration date", "May 4, 2026"),
    ]
    assert all(d["source_span"] in text for d in dates)


def test_ambiguous_open_date_label_date_sequence_is_withheld():
    assert printed_document_dates([chunk(
        "November 4, 2025\nEffective Date\nMay 4, 2026"
    )]) == []


def test_joined_reverse_footer_does_not_use_application_timestamp():
    dates = printed_document_dates([chunk(
        "11-04-2025Effective date: , 11-03-2025/02:18 PM CSTApplication date/time:"
    )])
    assert len(dates) == 1
    assert dates[0]["value"] == "11-04-2025"


def test_joined_reverse_label_with_unexplained_suffix_is_withheld():
    assert printed_document_dates([chunk(
        "11-04-2025Effective Date: estimated, not confirmed"
    )]) == []


def test_file_card_dates_come_only_from_its_own_document(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="dated-files", provider_thread_id="dated-files",
        subject="Email date is not policy date", body_text="Effective Date: January 1, 2001",
        attachments=[AttachmentPayload(
            provider_id="card", filename="card.txt", mime_type="text/plain",
            content=b"[Page 1]\nEffective Date\nSeptember 12, 2026\nExpiration Date\nMarch 12, 2027",
        ), AttachmentPayload(
            provider_id="other", filename="2022-09-01.txt", mime_type="text/plain",
            content=b"Another document without explicitly labelled dates",
        )],
    ))
    ids = {row["filename"]: row["id"] for row in repository.db.query(
        "SELECT id,filename FROM attachments"
    )}
    cards = repository.answer_file_cards([ids["card.txt"], ids["2022-09-01.txt"]], [])
    assert [(d["label"], d["value"]) for d in cards[0]["document_dates"]] == [
        ("Effective date", "September 12, 2026"),
        ("Expiration date", "March 12, 2027"),
    ]
    assert cards[1]["document_dates"] == []
    for date in cards[0]["document_dates"]:
        citation = repository.resolve_source_link(date["source_id"])
        assert citation[0] == cards[0]["thread_id"]
