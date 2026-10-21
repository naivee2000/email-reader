from __future__ import annotations

from email.message import EmailMessage

from email_reader.providers.mime import parse_rfc822


def test_rfc822_parsing_preserves_body_and_attachment() -> None:
    message = EmailMessage()
    message["From"] = "Maya Chen <maya@example.net>"
    message["To"] = "Owner <owner@example.com>"
    message["Subject"] = "Proposal"
    message["Message-ID"] = "<one@example.net>"
    message["Reply-To"] = "Service Desk <help@example.net>, Backup <backup@example.net>"
    message["References"] = "<root@example.net> <prior@example.net>"
    message.set_content("Please review the attached proposal.")
    message.add_attachment(
        b"contract", maintype="application", subtype="pdf", filename="proposal.pdf"
    )

    payload = parse_rfc822(
        message.as_bytes(),
        provider_id="provider-1",
        provider_thread_id="thread-1",
        labels=["UNREAD"],
    )
    assert payload.sender_email == "maya@example.net"
    assert payload.to == ["owner@example.com"]
    assert payload.reply_to == ["help@example.net", "backup@example.net"]
    assert payload.references == ["<root@example.net>", "<prior@example.net>"]
    assert payload.is_unread
    assert payload.attachments[0].filename == "proposal.pdf"
    assert payload.attachments[0].content == b"contract"


def test_plain_text_email_removes_layout_indent_but_keeps_small_indent() -> None:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "Owner <owner@example.com>"
    message["Subject"] = "Indented message"
    message.set_content(
        "                        Dear Owner,\n\n"
        "                        This paragraph came from an indented template.\n"
        "  - keep this intentional list indent\n"
    )

    payload = parse_rfc822(
        message.as_bytes(),
        provider_id="message-indented",
        provider_thread_id="thread-indented",
    )

    assert payload.body_text == (
        "Dear Owner,\n\n"
        "This paragraph came from an indented template.\n"
        "  - keep this intentional list indent"
    )


def test_html_document_mislabeled_as_plain_text_is_cleaned() -> None:
    message = EmailMessage()
    message["From"] = "Statements <statements@example.com>"
    message["To"] = "Owner <owner@example.com>"
    message["Subject"] = "Monthly statement"
    message.set_content(
        '<!doctype html><html><head><style>.hidden { display:none }</style></head>'
        '<body><h1>Your statement</h1><p>Balance: $25.00</p></body></html>'
    )

    payload = parse_rfc822(
        message.as_bytes(),
        provider_id="mislabeled-html",
        provider_thread_id="mislabeled-html-thread",
    )

    assert "<!doctype" not in payload.body_text.casefold()
    assert "Your statement" in payload.body_text
    assert "Balance: $25.00" in payload.body_text
    assert payload.body_html.startswith("<!doctype html>")


def test_wrapper_free_table_mislabeled_as_plain_text_is_renderable() -> None:
    message = EmailMessage()
    message["From"] = "Office <office@example.com>"
    message.set_content(
        '<table width="780"><tr><td><p>Appointment details</p>'
        '<img src="cid:logo"></td></tr></table>'
    )
    payload = parse_rfc822(message.as_bytes(), provider_id="m", provider_thread_id="t")
    assert payload.body_text == "Appointment details"
    assert payload.body_html.startswith('<table width="780">')


def test_isolated_table_tag_in_plain_message_stays_literal() -> None:
    message = EmailMessage()
    message.set_content("<table> is the opening tag in this HTML example.")
    payload = parse_rfc822(message.as_bytes(), provider_id="m", provider_thread_id="t")
    assert payload.body_text == "<table> is the opening tag in this HTML example."
    assert payload.body_html == ""


def test_prose_prefixed_html_template_and_broken_utf8_filler_are_cleaned() -> None:
    message = EmailMessage()
    message.set_content(
        "Open to learn whatâ\x80\x99s changed."
        + "<div>" * 8
        + "â\x80\x8c " * 30
        + '<a href="https://example.net/terms?tracking=long">Review terms</a>'
        + "</div>" * 8
    )
    payload = parse_rfc822(message.as_bytes(), provider_id="template", provider_thread_id="template")
    assert "what’s changed" in payload.body_text
    assert "Review terms" in payload.body_text
    assert "â\x80" not in payload.body_text
    assert "<div>" not in payload.body_text
    assert payload.body_html.startswith("Open to learn")
