from unittest.mock import Mock

from email_reader.ai import AssistantService, QueryPlan
from email_reader.config import AppSettings
from email_reader.contracts import Answer, AttachmentPayload, MessagePayload
from email_reader.search import HybridSearch, salient_bigrams


def test_answer_cleanup_removes_fullwidth_uuid_citations_not_user_content():
    identifier = "71c4a4e8-627f-5ee3-a856-9b99747b3ac9"
    text = f"Provide photos.【{identifier}】 Keep 【照片】 and policy ID {identifier}."
    assert AssistantService._clean_inline_source_ids(text) == (
        f"Provide photos. Keep 【照片】 and policy ID {identifier}."
    )
    assert AssistantService._clean_inline_source_ids(f"Supported. 【SOURCE: {identifier.upper()}】") == "Supported."


def test_inline_citations_survive_cleanup_and_supply_exact_file_card(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="card-citation", provider_thread_id="card-citation",
        body_text="Proof document attached.",
        attachments=[AttachmentPayload(provider_id="proof", filename="proof.pdf", mime_type="application/pdf")],
    ))
    search = HybridSearch(repository.db, repository)
    hit = next(item for item in search.conversational_search(["proof"])
               if item.citation.source_type == "attachment")
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Find proof", search_phrases=["proof"], response_type="file"),
        Answer(text=f"Proof is attached. 【SOURCE: {hit.chunk_id.upper()}】"),
    ]
    answer, _ = AssistantService(repository, search, ai, AppSettings()).answer("Find proof")
    assert answer.text == f"Proof is attached. [SOURCE:{hit.chunk_id}]"
    assert answer.source_ids == [hit.chunk_id]
    assert len(answer.attachment_ids) == 1
    assert repository.answer_file_cards(answer.attachment_ids, [])[0]["filename"] == "proof.pdf"
    assert ai.structured.call_count == 2


def test_inline_citations_do_not_promote_naked_ids_or_uncited_retrieval(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="markers", provider_thread_id="markers", body_text="Proof source.",
    ))
    hits = HybridSearch(repository.db, repository).conversational_search(["proof"])
    source = hits[0].chunk_id
    unknown = "00000000-0000-4000-8000-000000000000"
    answer = Answer(text=f"Policy identifier {source}. Unknown source [{unknown}].")
    AssistantService._preserve_inline_citations(answer, hits)
    assert answer.source_ids == []
    answer.text += f" Supported [{source}, {unknown}]. Duplicate \ue200cite\ue202{source}\ue201."
    AssistantService._preserve_inline_citations(answer, hits)
    assert answer.source_ids == [source]


def test_link_answer_uses_only_its_cited_sources_descriptive_label(repository):
    from bs4 import BeautifulSoup

    from email_reader.rendering import render_assistant_text

    url = "https://canvas.example.org/courses/2/files/17"
    repository.ingest_message("account-1", MessagePayload(
        provider_id="orientation-link", provider_thread_id="orientation-link",
        body_text=f"Review [Orientation Schedule] ({url}) before arrival.",
    ))
    search = HybridSearch(repository.db, repository)
    hit = next(item for item in search.conversational_search(["orientation schedule"])
               if url in item.content)
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Give me the orientation schedule link",
                  search_phrases=["orientation schedule"], response_type="link"),
        Answer(text=f"{url} [SOURCE:{hit.chunk_id}]"),
    ]
    answer, _ = AssistantService(repository, search, ai, AppSettings()).answer(
        "Give me the orientation schedule link"
    )
    assert answer.text == f"[Orientation Schedule]({url}) [SOURCE:{hit.chunk_id}]"
    soup = BeautifulSoup(render_assistant_text(
        answer.text, [{"source_id": hit.chunk_id}]
    ), "html.parser")
    assert [(link.get_text(), link["href"]) for link in soup.find_all("a")] == [
        ("Orientation Schedule", url),
        ("[1]", f"emailreader://source/{hit.chunk_id}"),
    ]
    assert "do not invent or alter URLs" in ai.structured.call_args_list[1].kwargs["instructions"]


def test_link_label_fallback_never_uses_uncited_or_different_target():
    from email_reader.search import SearchHit

    identifier = "71c4a4e8-627f-5ee3-a856-9b99747b3ac9"
    source = SearchHit(identifier, "[Different Document](https://example.org/other)", 1, Mock())
    answer = f"https://example.org/target [SOURCE:{identifier}]"
    assert AssistantService._label_single_cited_link(answer, [source], [identifier]) == answer
    source.content = "[Target Document](https://example.org/target)"
    assert AssistantService._label_single_cited_link(answer, [source], []) == answer
    assert AssistantService._label_single_cited_link(
        f"[Already Labelled](https://example.org/target) [SOURCE:{identifier}]",
        [source], [identifier],
    ) == f"[Already Labelled](https://example.org/target) [SOURCE:{identifier}]"


def test_claim_citations_keep_their_positions_as_safe_links(repository):
    from bs4 import BeautifulSoup

    from email_reader.rendering import ensure_answer_citations, render_assistant_text

    for number in (1, 2):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"claim-{number}", provider_thread_id=f"claim-{number}",
            body_text=f"Proof detail number {number}.",
        ))
    search = HybridSearch(repository.db, repository)
    hits = search.conversational_search(["proof"])
    first, second = hits[:2]
    unknown = "00000000-0000-4000-8000-000000000000"
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Proof", search_phrases=["proof"], response_type="information"),
        Answer(text=f"First claim.【SOURCE: {first.chunk_id.upper()}】\n\n"
                    f"Second claim. [Sources: {second.chunk_id}, {unknown}] [Page 2]"),
    ]
    answer, _ = AssistantService(repository, search, ai, AppSettings()).answer("proof")
    citations = [{"source_id": identifier} for identifier in answer.source_ids]
    markup = render_assistant_text(ensure_answer_citations(answer.text, citations), citations)
    soup = BeautifulSoup(markup, "html.parser")
    links = soup.find_all("a")
    assert [link["href"] for link in links] == [
        f"emailreader://source/{first.chunk_id}", f"emailreader://source/{second.chunk_id}",
    ]
    assert [link.get_text() for link in links] == ["[1]", "[2]"]
    assert markup.index("First claim.") < markup.index(links[0]["href"]) < markup.index("Second claim.")
    assert markup.index("Second claim.") < markup.index(links[1]["href"])
    assert first.chunk_id not in soup.get_text() and second.chunk_id not in soup.get_text()
    assert unknown not in markup
    assert "[Source unavailable]" in soup.get_text() and "[Page 2]" in soup.get_text()
    assert unknown not in answer.source_ids
    assert ai.structured.call_count == 2


def test_salient_bigrams_recall_topic_when_model_plan_is_too_broad(repository):
    for index in range(18):
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=f"life-{index}",
                provider_thread_id=f"life-{index}",
                subject="State Farm Life Application",
                body_text="State Farm life insurance application and exam appointment.",
            ),
        )
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="driving",
            provider_thread_id="driving",
            subject="STATEFARM DOCUMENTS",
            body_text="Defensive Driving Course: three drivers must complete the one-hour course.",
        ),
    )
    question = "What do I need to do for the State Farm defensive-driving discount?"
    assert "defensive driving" in salient_bigrams(question)
    hits = HybridSearch(repository.db, repository).conversational_search(
        [question, "State Farm"], limit=12
    )
    assert any(hit.citation.title == "STATEFARM DOCUMENTS" for hit in hits), [
        (hit.citation.title, hit.content[:50]) for hit in hits
    ]


def test_selective_translation_outweighs_many_broad_brand_reformulations(repository):
    for index in range(30):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"receipt-{index}", provider_thread_id=f"receipt-{index}",
            subject="Payment received",
            body_text="Example Insurance Company payment receipt billing account policy premium.",
        ))
    repository.ingest_message("account-1", MessagePayload(
        provider_id="specific-request", provider_thread_id="specific-request",
        subject="Requested materials",
        body_text="Example Insurance Company: 乐器保险，请提供乐器的照片。",
    ))
    queries = ["What did the agent request for musical instrument insurance?",
               "Example Insurance Company", "insurance company", "insurance payment",
               "payment receipt", "billing account", "policy premium", "乐器保险"]
    search = HybridSearch(repository.db, repository)
    hits = search.conversational_search(queries)
    assert hits[0].citation.title == "Requested materials"
    # Keep broad matches available as possible evidence; this is ranking, not
    # a hard-coded category filter or a loss of recall.
    assert any(hit.citation.title == "Payment received" for hit in hits)
    assert {hit.citation.account_email for hit in hits} == {"owner@example.com"}
    reversed_hits = search.conversational_search([queries[0], *reversed(queries[1:])])
    assert reversed_hits[0].chunk_id == hits[0].chunk_id


def test_chinese_covering_email_finds_original_documents_without_pdf_text(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="insurance",
            provider_thread_id="insurance-thread",
            subject="AUTO QUOTE",
            body_text="您的汽车保险资料已附上，包含保险凭证和保险卡。请查收。",
            attachments=[
                AttachmentPayload(
                    provider_id="binder",
                    filename="2022 BMW BINDER.pdf",
                    mime_type="application/pdf",
                    is_inline=True,
                ),
                AttachmentPayload(
                    provider_id="card", filename="2022 BMW ID CARD.pdf",
                    mime_type="application/pdf", is_inline=True,
                ),
                AttachmentPayload(
                    provider_id="logo",
                    filename="image001.png",
                    mime_type="image/png",
                    is_inline=True,
                ),
            ],
        ),
    )
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["auto insurance", "汽车保险", "保险卡"]
    )
    files = list(
        dict.fromkeys(
            hit.citation.title for hit in hits if hit.citation.source_type == "attachment"
        )
    )
    assert "2022 BMW BINDER.pdf" in files[:3]
    assert "2022 BMW ID CARD.pdf" in files[:3]
    assert all(hit.citation.sent_at and hit.citation.app_link for hit in hits)


def test_chinese_near_synonym_finds_attachment_covering_message(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="policy-synonym", provider_thread_id="policy-synonym",
            subject="保单资料", body_text="汽车保单和保障文件已附上，请查收。",
            attachments=[AttachmentPayload(
                provider_id="policy-file", filename="车辆保障.pdf",
                mime_type="application/pdf",
            )],
        ),
    )
    search = HybridSearch(repository.db, repository)
    hits = search.conversational_search(["汽车保险"], limit=12)
    assert any(hit.citation.title == "保单资料" for hit in hits)
    assert any(hit.citation.title == "车辆保障.pdf" for hit in hits)


def test_covering_message_keeps_non_image_document_with_at_sign_in_name(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="report-at-name", provider_thread_id="report-at-name",
            subject="Policy status", body_text="The current policy status report is attached.",
            attachments=[AttachmentPayload(
                provider_id="status-document", filename="status@agency.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ), AttachmentPayload(
                provider_id="signature-image", filename="signature@agency.png",
                mime_type="application/octet-stream", is_inline=True,
            )],
        ),
    )
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["current policy status report"], limit=12
    )
    assert any(hit.citation.title == "status@agency.docx" for hit in hits)
    assert all(hit.citation.title != "signature@agency.png" for hit in hits)


def test_exact_chinese_match_stays_first(repository):
    repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="exact-chinese", provider_thread_id="exact-chinese",
            subject="保险资料", body_text="汽车保险的正式文件已附上。",
        ),
    )
    repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="overlap-chinese", provider_thread_id="overlap-chinese",
            subject="保单资料", body_text="汽车保单的正式文件已附上。",
            attachments=[AttachmentPayload(
                provider_id="overlap-document", filename="车辆保单.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )],
        ),
    )
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["汽车保险"], limit=12,
    )
    assert hits[0].citation.title == "保险资料"
    assert any(hit.citation.title == "保单资料" for hit in hits)
    assert any(hit.citation.title == "车辆保单.docx" for hit in hits)


def test_follow_up_is_reformulated_and_unknown_attachment_ids_rejected(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="policy",
            provider_thread_id="policy",
            body_text="汽车保险凭证已经附上。",
            attachments=[
                AttachmentPayload(
                    provider_id="policyfile", filename="policy.pdf", mime_type="application/pdf"
                ),
            ],
        ),
    )
    search = HybridSearch(repository.db, repository)
    hits = search.conversational_search(["汽车保险"])
    file_hit = next(hit for hit in hits if hit.citation.source_type == "attachment")
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(
            standalone_question="Find the auto insurance PDF",
            search_phrases=["汽车保险"],
            response_type="file",
        ),
        Answer(
            text="Here is the attached policy document.",
            source_ids=[file_hit.chunk_id],
            attachment_ids=["invented-file", file_hit.chunk_id],
        ),
    ]
    history = [
        {"role": "user", "text": "Find my car insurance"},
        {"role": "assistant", "text": "I found a document."},
    ]
    answer, found = AssistantService(repository, search, ai, AppSettings()).answer(
        "Can you show me that PDF?", history=history
    )
    assert "Find my car insurance" in ai.structured.call_args_list[0].kwargs["input_text"]
    assert "Find the auto insurance PDF" in ai.structured.call_args_list[1].kwargs["input_text"]
    instructions = ai.structured.call_args_list[1].kwargs["instructions"]
    assert "one source requests an action while another says" in instructions
    assert "current status is unverified" in instructions
    assert len(answer.attachment_ids) == 1 and "invented-file" not in answer.attachment_ids
    assert answer.source_ids[0] in {hit.chunk_id for hit in found}


def test_attachment_metadata_backfill_is_idempotent(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="m",
            provider_thread_id="t",
            body_text="Your enrollment confirmation is attached.",
            attachments=[AttachmentPayload(provider_id="f", filename="enrollment.weird")],
        ),
    )
    repository.db.execute("DELETE FROM source_chunks WHERE source_type='attachment'")
    assert repository.backfill_attachment_metadata() == 1
    assert repository.backfill_attachment_metadata() == 0
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["enrollment confirmation"]
    )
    assert any(hit.citation.title == "enrollment.weird" for hit in hits)


def test_matching_file_brings_companion_documents_into_context(repository):
    repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="attachments", provider_thread_id="attachments", body_text="Please see attached.",
            attachments=[
                AttachmentPayload(provider_id="a", filename="automobile-insurance.pdf"),
                AttachmentPayload(provider_id="b", filename="proof-card.pdf"),
            ],
        ),
    )
    hits = HybridSearch(repository.db, repository).conversational_search(["automobile insurance"])
    assert {"automobile-insurance.pdf", "proof-card.pdf"}.issubset({hit.citation.title for hit in hits})


def test_filename_match_also_supplies_citable_text_from_inside_pdf(repository):
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Binder effective date: September 12, 2026")
    original = document.tobytes()
    document.close()
    repository.ingest_message("account-1", MessagePayload(
        provider_id="binder-document", provider_thread_id="binder-document",
        subject="Documents enclosed", body_text="Please see the attachment.",
        attachments=[AttachmentPayload(provider_id="binder", filename="auto insurance binder.pdf",
            mime_type="application/pdf", content=original, is_inline=True)],
    ))
    hits = HybridSearch(repository.db, repository).conversational_search(["auto insurance binder"])
    assert any(hit.citation.title == "auto insurance binder.pdf" and
               hit.citation.locator == "Page 1" and
               "September 12, 2026" in hit.content for hit in hits)


def test_short_attachment_context_includes_later_conditions_not_only_first_chunk(repository):
    text = ("[Page 1]\nCertificate introduction. " + "General description. " * 160
            + "\n\n[Page 2]\nSchedule and limits.\n\n"
            + "[Page 3]\nThis certificate ends after ninety days unless renewed.")
    repository.ingest_message("account-1", MessagePayload(
        provider_id="certificate", provider_thread_id="certificate", body_text="See enclosed.",
        attachments=[AttachmentPayload(provider_id="file", filename="membership certificate.txt",
                                       mime_type="text/plain", content=text.encode())],
    ))
    hits = HybridSearch(repository.db, repository).conversational_search(["membership certificate"])
    evidence = [hit for hit in hits if hit.citation.source_type == "attachment"
                and "ninety days" in hit.content]
    assert evidence and "Page 3" in evidence[0].citation.locator
    assert "not extracted file content" not in evidence[0].content
    assert len({hit.chunk_id for hit in hits}) == len(hits)


def test_long_attachment_expansion_keeps_matched_middle_and_boundaries_with_budget(repository):
    text = "\n\n".join(
        f"[Page {number}]\n" + ("Special renewal condition. " if number == 13 else "General material. ") * 150
        for number in range(1, 21)
    )
    repository.ingest_message("account-1", MessagePayload(
        provider_id="long", provider_thread_id="long", body_text="See enclosed.",
        attachments=[AttachmentPayload(provider_id="file", filename="long membership certificate.txt",
                                       mime_type="text/plain", content=text.encode())],
    ))
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["membership certificate", "special renewal condition"]
    )
    text_hits = [hit for hit in hits if hit.citation.source_type == "attachment"
                 and hit.citation.locator != "Attachment details and covering email"]
    assert len(text_hits) <= 8
    assert any(hit.citation.locator == "Page 1" for hit in text_hits)
    assert any("Page 20" in hit.citation.locator for hit in text_hits)
    assert any("Special renewal condition" in hit.content for hit in text_hits)


def test_filename_discovery_reaches_relevant_middle_page_without_exact_fts_phrase(repository):
    pages = []
    for number in range(1, 21):
        detail = (
            "Renewal deadline: November 17, 2026."
            if number == 13 else "General membership terms."
        )
        pages.append(f"[Page {number}]\n{detail} " + "Background information. " * 140)
    repository.ingest_message("account-1", MessagePayload(
        provider_id="middle-page", provider_thread_id="middle-page",
        subject="Documents enclosed", body_text="Please see the attachment.",
        attachments=[AttachmentPayload(
            provider_id="file", filename="membership certificate.txt",
            mime_type="text/plain", content="\n\n".join(pages).encode(),
        )],
    ))
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["membership certificate renewal"]
    )
    pages_found = [hit for hit in hits if hit.citation.title == "membership certificate.txt"
                   and hit.citation.locator != "Attachment details and covering email"]
    assert len(pages_found) <= 8
    assert any(hit.citation.locator == "Page 1" for hit in pages_found)
    assert any(hit.citation.locator == "Page 20" for hit in pages_found)
    assert any(hit.citation.locator == "Page 13" and "November 17, 2026" in hit.content
               for hit in pages_found), [
        (hit.citation.locator, "November 17, 2026" in hit.content) for hit in pages_found
    ]


def test_attachment_locator_backfill_preserves_ids_text_embeddings_and_metadata(repository):
    text = "[Page 1]\n" + "Opening section. " * 200 + "\n\n[Page 2]\nFinal condition."
    repository.ingest_message("account-1", MessagePayload(
        provider_id="labels", provider_thread_id="labels", body_text="See enclosed.",
        attachments=[AttachmentPayload(provider_id="file", filename="label example.txt",
                                       mime_type="text/plain", content=text.encode())],
    ))
    repository.db.execute("UPDATE source_chunks SET locator='Page 1',embedding_json='[1,2]' "
                          "WHERE source_type='attachment' AND chunk_index>=0")
    before = [dict(row) for row in repository.db.query(
        "SELECT id,content,embedding_json FROM source_chunks WHERE source_type='attachment' ORDER BY id"
    )]
    assert repository.backfill_attachment_locators() > 0
    assert repository.backfill_attachment_locators() == 0
    after = [dict(row) for row in repository.db.query(
        "SELECT id,content,embedding_json FROM source_chunks WHERE source_type='attachment' ORDER BY id"
    )]
    assert after == before
    assert repository.db.scalar("SELECT locator FROM source_chunks WHERE source_type='attachment' AND chunk_index=-1") == (
        "Attachment details and covering email"
    )
    assert any("Page 2" in row["locator"] for row in repository.db.query(
        "SELECT locator FROM source_chunks WHERE source_type='attachment' AND chunk_index>=0"
    ))


def test_continuation_and_cross_page_sheet_slide_labels(repository):
    assert repository._attachment_chunk_locators([
        "[Page 1]\nFirst part", "continued text", "end of first\n[Page 2]\nSecond",
        "[Page 3]\nThird", "[Sheet: Budget]\nRows", "more rows\n[Sheet: Notes]\nNotes",
        "[Slide 1]\nTitle", "[Slide 2]\nNext",
    ]) == ["Page 1", "Page 1", "Page 1, Page 2", "Page 3", "Sheet: Budget",
           "Sheet: Budget, Sheet: Notes", "Slide 1", "Slide 2"]


def test_inline_pdf_can_be_cited_and_opened_as_answer_file(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="inline-policy", provider_thread_id="inline-policy",
        body_text="Your auto insurance proof is attached.",
        attachments=[AttachmentPayload(provider_id="proof", filename="auto proof.pdf",
            mime_type="application/pdf", is_inline=True, content=b"saved original")],
    ))
    search = HybridSearch(repository.db, repository)
    hits = search.conversational_search(["auto insurance proof"])
    file_hit = next(hit for hit in hits if hit.citation.title == "auto proof.pdf")
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Find my auto insurance proof",
                  search_phrases=["auto insurance proof"], response_type="file"),
        Answer(text="Here is the proof file.", source_ids=[file_hit.chunk_id],
               attachment_ids=[file_hit.chunk_id]),
    ]
    answer, _ = AssistantService(repository, search, ai, AppSettings()).answer(
        "Find my auto insurance proof"
    )
    cards = repository.answer_file_cards(answer.attachment_ids, answer.related_attachment_ids)
    assert [card["filename"] for card in cards] == ["auto proof.pdf"]
    assert cards[0]["available"]


def test_bare_topic_with_cited_original_is_not_marked_incomplete_due_to_live_status(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="dated-policy", provider_thread_id="dated-policy",
        body_text="Auto insurance binder is attached. Current active status is not stated.",
        attachments=[AttachmentPayload(provider_id="binder", filename="binder.pdf",
            mime_type="application/pdf", content=b"preserved original")],
    ))
    search = HybridSearch(repository.db, repository)
    file_hit = next(hit for hit in search.conversational_search(["auto insurance"])
                    if hit.citation.source_type == "attachment")
    for question, expected_incomplete in (
        ("auto insurance", False),
        ("Is my auto insurance active?", True),
        ("insurance status", True),
    ):
        ai = Mock()
        ai.structured.side_effect = [
            QueryPlan(standalone_question=question, search_phrases=["auto insurance"],
                      response_type="information"),
            Answer(text="The dated binder is attached, but present active status is unverified. "
                        f"[SOURCE:{file_hit.chunk_id}]",
                   source_ids=[file_hit.chunk_id], insufficient_evidence=True),
        ]
        answer, _ = AssistantService(repository, search, ai, AppSettings()).answer(question)
        assert answer.insufficient_evidence is expected_incomplete
        assert answer.attachment_ids
        assert "unverified" in answer.text


def test_bare_topic_without_available_original_keeps_incomplete_flag(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="missing-original", provider_thread_id="missing-original",
        body_text="Auto insurance binder was mentioned.",
        attachments=[AttachmentPayload(provider_id="binder", filename="binder.pdf",
            mime_type="application/pdf")],
    ))
    search = HybridSearch(repository.db, repository)
    file_hit = next(hit for hit in search.conversational_search(["auto insurance"])
                    if hit.citation.source_type == "attachment")
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Auto insurance", search_phrases=["auto insurance"],
                  response_type="file"),
        Answer(text="The binder is listed, but the original cannot be opened from this mailbox. "
                    f"[SOURCE:{file_hit.chunk_id}]",
               source_ids=[file_hit.chunk_id], insufficient_evidence=True),
    ]
    answer, _ = AssistantService(repository, search, ai, AppSettings()).answer("auto insurance")
    assert answer.insufficient_evidence


def test_legacy_signature_metadata_cannot_flood_document_search(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="sig-noise", provider_thread_id="sig-noise", body_text="Auto insurance information.",
        attachments=[AttachmentPayload(provider_id=str(i), filename=f"image{i}.png", mime_type="image/png") for i in range(50)],
    ))
    repository.ingest_message("account-1", MessagePayload(
        provider_id="documents", provider_thread_id="documents", body_text="Auto insurance information.",
        attachments=[AttachmentPayload(provider_id="policy", filename="policy.pdf", mime_type="application/pdf"),
                     AttachmentPayload(provider_id="card", filename="card.pdf", mime_type="application/pdf")],
    ))
    hits = HybridSearch(repository.db, repository).conversational_search(["auto insurance"])
    titles = [hit.citation.title for hit in hits if hit.citation.source_type == "attachment"]
    assert {"policy.pdf", "card.pdf"}.issubset(set(titles[:3]))
    explicit = HybridSearch(repository.db, repository).search("image10.png")
    assert any(hit.citation.title == "image10.png" for hit in explicit)


def test_recall_preserves_other_retrieved_files_without_calling_them_confirmed(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="recall", provider_thread_id="recall", body_text="Your insurance documents are attached.",
        attachments=[AttachmentPayload(provider_id=name, filename=name, mime_type="application/pdf")
            for name in ["policy.pdf", "card.pdf", "quote.pdf", "endorsement.pdf"]],
    ))
    search = HybridSearch(repository.db, repository)
    found = search.conversational_search(["insurance"])
    first = next(hit for hit in found if hit.citation.source_type == "attachment")
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Find insurance documents", search_phrases=["insurance"], response_type="file"),
        Answer(text=f"One matching document. [SOURCE:{first.chunk_id}]", source_ids=[first.chunk_id], attachment_ids=[first.chunk_id],
               related_source_ids=["invented"], related_attachment_ids=["invented"]),
    ]
    answer, hits = AssistantService(repository, search, ai, AppSettings()).answer("insurance")
    assert len(answer.attachment_ids) == 1
    assert len(answer.related_attachment_ids) == 3
    assert not set(answer.attachment_ids).intersection(answer.related_attachment_ids)
    assert set(answer.related_source_ids).issubset({hit.chunk_id for hit in hits})
    assert "invented" not in answer.related_attachment_ids
    assert answer.text == f"One matching document. [SOURCE:{first.chunk_id}]"
    assert answer.source_ids == [first.chunk_id]
    answer_request = ai.structured.call_args_list[1].kwargs
    assert "CURRENT LOCAL DATE:" in answer_request["input_text"]
    assert "was scheduled for" in answer_request["instructions"]
    assert "A date passing does not prove" in answer_request["instructions"]
    assert "Do not copy future tense from historical announcements" in answer_request["instructions"]
    assert "attribute its venue or instructions" in answer_request["instructions"]
    assert "does not confirm actual delivery" in answer_request["instructions"]
    assert "actually requested cannot be verified" in answer_request["instructions"]
    assert "optional follow-through" in answer_request["instructions"]
    assert "present-day status cannot be confirmed" in answer_request["instructions"]
    assert "an applied benefit does not prove the requested task was completed" in answer_request["instructions"]
    assert "side by side with separate citations" in answer_request["instructions"]
    assert "Each factual paragraph or bullet" in answer_request["instructions"]
    assert "cite every supporting document" in answer_request["instructions"]
    assert "retrieval rank is not chronology" in answer_request["instructions"]
    assert "timezone offsets" in answer_request["instructions"]
    assert "dates are absent or ambiguous" in answer_request["instructions"]


def test_source_context_includes_exact_timestamps_without_using_retrieval_order(repository):
    import json
    from datetime import datetime

    for identifier, date in [("later", "2026-09-13T02:26:14+00:00"),
                             ("earlier", "2026-09-10T08:43:35-04:00")]:
        repository.ingest_message("account-1", MessagePayload(
            provider_id=identifier, provider_thread_id=identifier,
            subject="Parking agreement", sender_email="operator@example.com",
            sent_at=datetime.fromisoformat(date), body_text="Payment setup is separate.",
        ))
    hits = HybridSearch(repository.db, repository).conversational_search(["parking agreement"])
    hits.sort(key=lambda hit: hit.citation.sent_at, reverse=True)
    context = AssistantService._hits_context(hits)
    metadata = [json.loads(line.removeprefix("SOURCE_METADATA="))
                for line in context.splitlines() if line.startswith("SOURCE_METADATA=")]
    assert len(metadata) == len(hits) == 2
    assert [item["sent_at"] for item in metadata] == [hit.citation.sent_at for hit in hits]
    assert all(item["sender"] == "operator@example.com" for item in metadata)
    assert all(item["account_email"] == "owner@example.com" for item in metadata)
    for hit in hits:
        assert f"[SOURCE:{hit.chunk_id}]" in context and hit.content in context


def test_uncited_answer_still_exposes_matches_as_unconfirmed(repository):
    repository.ingest_message("account-1", MessagePayload(provider_id="uncited", provider_thread_id="uncited",
        body_text="Insurance quote attached", attachments=[AttachmentPayload(provider_id="file", filename="quote.pdf")]))
    ai = Mock()
    ai.structured.side_effect = [
        QueryPlan(standalone_question="Insurance", search_phrases=["insurance"], response_type="file"),
        Answer(text="An unsupported confident claim", source_ids=["made-up"]),
    ]
    answer, hits = AssistantService(repository, HybridSearch(repository.db, repository), ai, AppSettings()).answer("insurance")
    assert answer.insufficient_evidence and "unsupported confident claim" not in answer.text
    assert answer.source_ids == answer.attachment_ids == []
    assert answer.related_attachment_ids and answer.related_source_ids


def test_natural_question_matches_filename_without_requiring_body_or_model_phrase(repository):
    repository.ingest_message("account-1", MessagePayload(provider_id="target", provider_thread_id="target",
        subject="Unrelated covering subject", body_text="Please see the enclosed documents.",
        attachments=[AttachmentPayload(provider_id="card", filename="2031 ACME ID CARD.pdf", mime_type="application/pdf")]))
    for index in range(45):
        repository.ingest_message("account-1", MessagePayload(provider_id=f"noise-{index}", provider_thread_id=f"noise-{index}",
            subject=f"Other insurance {index}", body_text="An insurance quote is available."))
    hits = HybridSearch(repository.db, repository).conversational_search(
        ["Can you find the insurance ID card for my 2031 ACME?", "insurance quote"])
    assert hits[0].citation.title == "2031 ACME ID CARD.pdf"


def test_natural_question_matches_subject_only_and_keeps_scope_and_trash(repository):
    from email_reader.contracts import Account, ProviderKind
    repository.upsert_account(Account(id="other", email="other@example.com", provider=ProviderKind.OUTLOOK))
    for account in ["account-1", "other"]:
        repository.ingest_message(account, MessagePayload(provider_id="parking", provider_thread_id="parking",
            subject="Complete with Docusign: 555 Monthly Parking Agreement", body_text="The operator will contact you."))
    search = HybridSearch(repository.db, repository)
    query = "Find the email about my 555 monthly parking agreement and who will contact me about payment."
    hits = search.conversational_search([query], account_id="account-1")
    assert hits and hits[0].citation.title.endswith("555 Monthly Parking Agreement")
    assert {hit.citation.account_email for hit in hits} == {"owner@example.com"}
    repository.db.execute("UPDATE threads SET trashed_at=? WHERE account_id='account-1'", (repository.db.now(),))
    assert search.conversational_search([query], account_id="account-1") == []


def test_explicit_search_finds_imported_filtered_file_without_unhiding_inbox(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="filtered-policy", provider_thread_id="filtered-policy",
        labels=["INBOX", "CATEGORY_PROMOTIONS"],
        subject="Insurance renewal information",
        body_text="Your insurance renewal information is attached.",
        attachments=[AttachmentPayload(
            provider_id="filtered-binder", filename="renewal binder.pdf",
            mime_type="application/pdf",
        )],
    ))
    assert repository.list_threads(mailbox="inbox") == []

    search = HybridSearch(repository.db, repository)
    hits = search.conversational_search(["insurance renewal binder"])
    file_hit = next(hit for hit in hits if hit.citation.title == "renewal binder.pdf")
    attachment_id = repository.db.scalar(
        "SELECT source_id FROM source_chunks WHERE id=?", (file_hit.chunk_id,)
    )
    cards = repository.answer_file_cards([attachment_id], [])
    assert [card["filename"] for card in cards] == ["renewal binder.pdf"]
    thread_id = repository.db.scalar(
        "SELECT thread_id FROM source_chunks WHERE id=?", (file_hit.chunk_id,)
    )
    assert len(repository.list_threads(mailbox="all", thread_ids=[thread_id])) == 1

    repository.trash_thread(thread_id)
    assert search.conversational_search(["insurance renewal binder"]) == []
    assert repository.answer_file_cards([attachment_id], []) == []


def test_cited_original_gets_file_card_even_if_model_omits_attachment_field(repository):
    repository.ingest_message("account-1", MessagePayload(provider_id="cited", provider_thread_id="cited",
        body_text="Insurance documents attached", attachments=[
            AttachmentPayload(provider_id="binder", filename="binder.pdf", mime_type="application/pdf"),
            AttachmentPayload(provider_id="card", filename="card.pdf", mime_type="application/pdf"),
            AttachmentPayload(provider_id="extra", filename="extra.pdf", mime_type="application/pdf")]))
    search = HybridSearch(repository.db, repository)
    found = search.conversational_search(["insurance"])
    cited = [hit for hit in found if hit.citation.source_type == "attachment" and hit.citation.title in {"binder.pdf", "card.pdf"}]
    ai = Mock()
    ai.structured.side_effect = [QueryPlan(standalone_question="Insurance", search_phrases=["insurance"], response_type="file"),
        Answer(text="These are the policy and proof documents.", source_ids=[hit.chunk_id for hit in cited], attachment_ids=[])]
    answer, _ = AssistantService(repository, search, ai, AppSettings()).answer("insurance")
    assert len(answer.attachment_ids) == 2
    assert len(answer.related_attachment_ids) == 1
