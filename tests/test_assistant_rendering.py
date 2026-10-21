from bs4 import BeautifulSoup

from email_reader.contracts import AttachmentPayload, MessagePayload
from email_reader.rendering import (
    audited_summary,
    ensure_answer_citations,
    render_assistant_text,
    summary_citations,
)

SOURCE = "01234567-89ab-cdef-0123-456789abcdef"


def test_valid_citation_is_numbered_link_with_safe_emphasis_and_line_breaks():
    value = render_assistant_text(f"**Document dates**<br />These are recorded dates. [{SOURCE}]", [{"source_id": SOURCE}])
    soup = BeautifulSoup(value, "html.parser")
    assert soup.strong.text == "Document dates"
    assert soup.br is not None
    assert soup.a.text == "[1]"
    assert soup.a["href"] == "emailreader://source/" + SOURCE
    assert SOURCE not in soup.get_text()


def test_answer_without_inline_marker_adds_only_validated_source_link():
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    original = "The meeting is tomorrow."
    cited = ensure_answer_citations(
        original,
        [{"source_id": SOURCE}, {"source_id": other}, {"source_id": "javascript:bad"}],
    )
    soup = BeautifulSoup(render_assistant_text(cited, [{"source_id": SOURCE}, {"source_id": other}]),
                         "html.parser")
    assert soup.get_text().startswith(original)
    assert [link.text for link in soup.find_all("a")] == ["[1]", "[2]"]
    assert [link["href"] for link in soup.find_all("a")] == [
        "emailreader://source/" + SOURCE,
        "emailreader://source/" + other,
    ]
    assert "javascript:" not in cited


def test_answer_with_native_citation_does_not_duplicate_it():
    original = f"The meeting is tomorrow. cite{SOURCE}"
    assert ensure_answer_citations(original, [{"source_id": SOURCE}]) == original
    assert ensure_answer_citations("No verified answer.", []) == "No verified answer."


def test_unknown_reference_and_model_html_never_gain_trusted_links_or_resources():
    value = render_assistant_text(f'[{SOURCE}] <img src="https://example.com/tracker"> **<script>bad</script>**', [])
    soup = BeautifulSoup(value, "html.parser")
    assert soup.img is None and soup.script is None
    assert "Source unavailable" in soup.get_text()
    assert SOURCE not in soup.get_text()
    assert not any(a.get("href", "").startswith("emailreader:") for a in soup.find_all("a"))


def test_citation_numbers_follow_source_list_and_ignore_supplied_unsafe_url():
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    value = render_assistant_text(f"[SOURCE: {SOURCE}] [Document](https://example.com/doc)",
        [{"source_id": other}, {"source_id": SOURCE, "app_link": "javascript:bad()"}])
    soup = BeautifulSoup(value, "html.parser")
    assert soup.find("a", href="emailreader://source/" + SOURCE).text == "[2]"
    assert soup.find("a", href="https://example.com/doc").text == "Document"
    assert "javascript:" not in value


def test_answer_link_label_hides_stray_control_character_without_losing_source():
    answer = (
        "[Driving course \x7f information](https://example.net/course) "
        f"supports this detail. [SOURCE: {SOURCE}]"
    )
    soup = BeautifulSoup(
        render_assistant_text(answer, [{"source_id": SOURCE}]), "html.parser"
    )
    assert [(link.text, link["href"]) for link in soup.find_all("a")] == [
        ("Driving course information", "https://example.net/course"),
        ("[1]", "emailreader://source/" + SOURCE),
    ]
    assert "\x7f" not in soup.get_text()


def test_codex_citation_tokens_become_numbered_links_and_unknowns_are_not_trusted():
    second = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    rendered = render_assistant_text(
        f"Evidence cite{SOURCE}{second}<br/>More citeffffffff-ffff-ffff-ffff-ffffffffffff",
        [{"source_id": second}, {"source_id": SOURCE}],
    )
    soup = BeautifulSoup(rendered, "html.parser")
    links = [(item.text, item["href"]) for item in soup.find_all("a")]
    assert links == [("[2]", "emailreader://source/" + SOURCE),
                     ("[1]", "emailreader://source/" + second)]
    assert "Source unavailable" in soup.get_text()
    assert "cite" not in rendered
    assert soup.br is not None


def test_cached_summary_message_and_attachment_links_resolve_to_exact_originals(repository):
    message_id = repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="summary-source",
            provider_thread_id="summary-thread",
            body_text="The orientation begins on October 1.",
            attachments=[AttachmentPayload(provider_id="agenda", filename="agenda.pdf",
                                           mime_type="application/pdf")],
        ),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    thread = repository.get_thread(thread_id)
    message = thread["messages"][0]
    attachment = message["attachments"][0]
    unknown = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    summary = {
        "key_points": [f"Orientation begins October 1. [{message['id']}]"],
        "important_attachments": [f"The agenda is attached. [{attachment['id']}]"],
        "source_ids": [unknown, message["id"], attachment["id"]],
    }
    citations = summary_citations(summary, thread)
    assert [item["source_id"] for item in citations] == [message["id"], attachment["id"]]
    html = render_assistant_text(summary["key_points"][0], citations)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.a.text == "[1]"
    assert soup.a["href"] == "emailreader://source/" + message["id"]
    assert message["id"] not in soup.get_text()
    assert repository.resolve_source_link(message["id"]) == (thread_id, message["id"])
    assert repository.resolve_source_link(attachment["id"]) == (thread_id, message["id"])
    chunk_id = repository.db.scalar(
        "SELECT id FROM source_chunks WHERE source_type='message' AND source_id=?", (message["id"],)
    )
    assert repository.resolve_source_link(chunk_id) == (thread_id, message["id"])
    assert repository.resolve_source_link(unknown) is None


def test_summary_rejects_forged_cross_thread_source_ids(repository):
    thread_ids = []
    for value in ("one", "two"):
        message_id = repository.ingest_message("account-1", MessagePayload(
            provider_id=value, provider_thread_id=value, body_text=value,
        ))
        thread_ids.append(repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,)))
    threads = [repository.get_thread(thread_id) for thread_id in thread_ids]
    foreign_id = threads[1]["messages"][0]["id"]
    citations = summary_citations({"key_points": [f"Unsupported [{foreign_id}]"],
                                   "source_ids": [foreign_id]}, threads[0])
    assert citations == []
    html = render_assistant_text(f"Unsupported [{foreign_id}]", citations)
    assert "Source unavailable" in BeautifulSoup(html, "html.parser").get_text()
    assert foreign_id not in html


def test_summary_hides_uncited_and_foreign_claims_but_keeps_valid_cached_bullets(repository):
    own_message = repository.ingest_message(
        "account-1", MessagePayload(provider_id="own", provider_thread_id="own",
                                     body_text="Meeting is October 1."),
    )
    other_message = repository.ingest_message(
        "account-1", MessagePayload(provider_id="other", provider_thread_id="other",
                                     body_text="Unrelated text."),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (own_message,))
    thread = repository.get_thread(thread_id)
    raw = {
        "key_points": [f"Meeting is October 1. [{own_message}]", "Uncited claim."],
        "decisions": [f"Forged decision. [{other_message}]"],
        "dates_and_deadlines": [], "action_items": [], "important_attachments": [],
        "source_ids": [own_message, other_message],
    }
    shown = audited_summary(raw, thread)
    assert shown["key_points"] == [raw["key_points"][0]]
    assert shown["decisions"] == []
    assert shown["omitted_count"] == 2
    assert shown["citations"] == [{"source_id": own_message}]
    assert raw["key_points"][1] == "Uncited claim.", "Cached model output stays intact"


def test_global_source_list_cannot_substitute_for_per_bullet_citation(repository):
    message_id = repository.ingest_message(
        "account-1", MessagePayload(provider_id="single", provider_thread_id="single",
                                     body_text="Source text."),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    result = audited_summary({"key_points": ["No inline source."], "source_ids": [message_id]},
                             repository.get_thread(thread_id))
    assert result["key_points"] == []
    assert result["citations"] == []
    assert result["omitted_count"] == 1


def test_summary_accepts_attachment_and_native_citation_forms(repository):
    message_id = repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="file-source", provider_thread_id="file-source",
            body_text="Agenda is attached.",
            attachments=[AttachmentPayload(provider_id="agenda-2", filename="agenda.pdf",
                                           mime_type="application/pdf")],
        ),
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    thread = repository.get_thread(thread_id)
    attachment_id = thread["messages"][0]["attachments"][0]["id"]
    summary = {"key_points": [f"Agenda is attached. cite{message_id}"],
               "important_attachments": [f"Agenda PDF. [ATTACHMENT:{attachment_id}]"]}
    audited = audited_summary(summary, thread)
    assert audited["omitted_count"] == 0
    assert [item["source_id"] for item in audited["citations"]] == [message_id, attachment_id]
    rendered = render_assistant_text(summary["important_attachments"][0], audited["citations"])
    soup = BeautifulSoup(rendered, "html.parser")
    assert soup.a.text == "[2]"
    assert soup.a["href"] == "emailreader://source/" + attachment_id
