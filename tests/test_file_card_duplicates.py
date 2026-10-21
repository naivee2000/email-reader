from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind


def add_file(repository, provider_id, filename, content, account="account-1"):
    repository.ingest_message(account, MessagePayload(
        provider_id=provider_id, provider_thread_id=provider_id, subject="Documents",
        body_text="Attached original",
        attachments=[AttachmentPayload(provider_id=provider_id, filename=filename,
                                       mime_type="application/pdf", content=content)],
    ))
    return repository.db.scalar(
        "SELECT a.id FROM attachments a JOIN messages m ON m.id=a.message_id "
        "WHERE m.account_id=? AND m.provider_id=?", (account, provider_id)
    )


def test_exact_duplicate_cards_prefer_descriptive_name_and_keep_cited_relationship(repository):
    binder = add_file(repository, "generic-binder", "Binder.pdf", b"binder original")
    card = add_file(repository, "generic-card", "Temporary Auto ID Card.pdf", b"id original")
    quote = add_file(repository, "quote", "Quote.pdf", b"quote original")
    named_binder = add_file(repository, "named-binder", "2024 VehicleZ9 BINDER.pdf", b"binder original")
    named_card = add_file(repository, "named-card", "2024 VehicleZ9 ID CARD.pdf", b"id original")
    cards = repository.answer_file_cards([binder, card, quote], [named_card, named_binder])
    assert [c["id"] for c in cards] == [named_binder, named_card, quote]
    assert all(c["possible_match"] and c["identical_original_cited"] for c in cards[:2])
    assert cards[0]["duplicate_sources"][0]["id"] == binder
    assert cards[1]["duplicate_sources"][0]["id"] == card
    for result in cards[:2]:
        alias = result["duplicate_sources"][0]
        assert alias["cited"]
        assert repository.resolve_source_link(alias["id"]) == (
            alias["thread_id"], alias["message_id"]
        )
        assert "content_digest" not in result and "account_id" not in result


def test_similar_names_and_similar_text_are_not_exact_duplicates(repository):
    first = add_file(repository, "a", "Binder.pdf", b"version 1")
    second = add_file(repository, "b", "VehicleZ9 Binder.pdf", b"version 2")
    assert [c["id"] for c in repository.answer_file_cards([first], [second])] == [first, second]


def test_duplicate_files_never_merge_across_accounts(repository):
    repository.upsert_account(Account(
        id="account-2", provider=ProviderKind.OUTLOOK, email="other@example.net"
    ))
    first = add_file(repository, "a", "Binder.pdf", b"same bytes")
    second = add_file(repository, "b", "VehicleZ9 Binder.pdf", b"same bytes", "account-2")
    cards = repository.answer_file_cards([first], [second])
    assert len(cards) == 2
    assert not any(c["duplicate_sources"] for c in cards)


def test_unavailable_originals_are_not_merged_and_keep_disabled_open_state(repository):
    first = add_file(repository, "a", "Binder.pdf", b"first")
    second = add_file(repository, "b", "VehicleZ9 Binder.pdf", b"second")
    repository.db.execute("UPDATE attachments SET content_digest=NULL")
    cards = repository.answer_file_cards([first], [second])
    assert len(cards) == 2
    assert not any(c["available"] or c["duplicate_sources"] for c in cards)


def test_same_account_duplicate_sources_survive_in_source_specific_order(repository):
    first = add_file(repository, "a", "VehicleZ9 Binder.pdf", b"same")
    second = add_file(repository, "b", "Binder.pdf", b"same")
    cards = repository.answer_file_cards([first], [second])
    assert len(cards) == 1 and cards[0]["id"] == first
    assert not cards[0]["possible_match"]
    assert not cards[0]["identical_original_cited"]
    assert cards[0]["duplicate_sources"][0]["id"] == second
    assert not cards[0]["duplicate_sources"][0]["cited"]


def test_named_separate_originals_rank_without_citation_promotion(repository):
    binder = add_file(repository, "generic-binder", "Binder.pdf", b"generic binder")
    card = add_file(repository, "generic-card", "Temporary Auto ID Card.pdf", b"generic id")
    quote = add_file(repository, "quote", "Quote.pdf", b"quote")
    named_card = add_file(repository, "named-card", "2024 VehicleZ9 ID CARD.pdf", b"named id")
    named_binder = add_file(repository, "named-binder", "2024 VehicleZ9 BINDER.pdf", b"named binder")
    cards = repository.answer_file_cards(
        [binder, card, quote], [named_card, named_binder], question="auto insurance"
    )
    assert [c["id"] for c in cards] == [named_card, named_binder, binder, card, quote]
    assert all(c["possible_match"] for c in cards[:2])
    assert not any(c["identical_original_cited"] or c["duplicate_sources"] for c in cards)
    assert len(cards) == 5, "Different originals and their sources must survive"


def test_explicit_generic_filename_keeps_requested_source_first(repository):
    generic = add_file(repository, "generic", "Binder.pdf", b"generic")
    named = add_file(repository, "named", "2024 VehicleZ9 Binder.pdf", b"named")
    cards = repository.answer_file_cards([generic], [named], question="Open Binder.pdf")
    assert cards[0]["id"] == generic


def test_explicit_filename_selects_exact_duplicate_alias_requested_by_user(repository):
    generic = add_file(repository, "generic", "Binder.pdf", b"same")
    named = add_file(repository, "named", "2024 VehicleZ9 Binder.pdf", b"same")
    cards = repository.answer_file_cards([named], [generic], question="Open Binder.pdf")
    assert cards[0]["id"] == generic
    assert cards[0]["duplicate_sources"][0]["id"] == named


def test_unified_document_ranking_preserves_sources_and_untyped_positions(repository):
    repository.upsert_account(Account(
        id="account-2", provider=ProviderKind.OUTLOOK, email="other@example.net"
    ))
    generic = add_file(repository, "generic", "Binder.pdf", b"generic")
    named = add_file(repository, "named", "2024 VehicleZ9 Binder.pdf", b"named", "account-2")
    other = add_file(repository, "other", "Zoological Review.pdf", b"other")
    cards = repository.answer_file_cards([generic, other], [named], question="auto insurance")
    assert [c["id"] for c in cards] == [named, other, generic]
    assert repository.resolve_source_link(cards[0]["id"])[0] == cards[0]["thread_id"]


def test_unavailable_named_original_does_not_displace_available_generic(repository):
    generic = add_file(repository, "generic", "Binder.pdf", b"generic")
    named = add_file(repository, "named", "2024 VehicleZ9 Binder.pdf", b"named")
    repository.db.execute("UPDATE attachments SET content_digest=NULL WHERE id=?", (named,))
    cards = repository.answer_file_cards([generic], [named], question="auto insurance")
    assert cards[0]["id"] == generic


def test_policy_documents_can_move_above_multiple_quotes_without_merging(repository):
    ids = [add_file(repository, str(index), filename, str(index).encode()) for index, filename in enumerate([
        "Temporary Auto ID Card.pdf", "NI_QUOTE.pdf", "Personal+Auto+Quote.pdf", "Binder.pdf",
        "2024 VehicleZ9 ID CARD.pdf", "2024 VehicleZ9 BINDER.pdf",
    ])]
    # Real providers may identify a PDF by extension but report generic MIME.
    repository.db.execute("UPDATE attachments SET mime_type='application/octet-stream' WHERE id=?", (ids[-1],))
    cards = repository.answer_file_cards(ids[:4], ids[4:], question="auto insurance")
    assert {c["id"] for c in cards[:2]} == set(ids[4:])
    assert len(cards) == 6 and all(c["possible_match"] for c in cards[:2])


def test_explicit_quote_request_overrides_policy_document_preference(repository):
    binder = add_file(repository, "binder", "VehicleZ9 Binder.pdf", b"binder")
    quote = add_file(repository, "quote", "Quote.pdf", b"quote")
    cards = repository.answer_file_cards([binder], [quote], question="Find my quotes")
    assert cards[0]["id"] == quote
    assert cards[0]["possible_match"]
