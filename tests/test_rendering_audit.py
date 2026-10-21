from email_reader.diagnostics import rendered_link_gaps
from email_reader.rendering import render_message


def test_rendered_mail_audit_counts_links_without_reporting_targets():
    rendered = render_message(
        "Open [Schedule](https://example.org/very/long/path), email help@example.org, "
        "and see [cid:logo@example.org].",
        "",
        [{"id": "logo", "provider_id": "logo", "content_id": "logo@example.org",
          "filename": "logo.png", "mime_type": "image/png", "content_digest": "digest"}],
    )
    assert rendered_link_gaps(rendered.html) == {
        "raw_cid_text": 0,
        "bare_web_text": 0,
        "bare_email_text": 0,
        "external_image_sources": 0,
        "clickable_links": 2,
    }


def test_rendered_mail_audit_detects_visible_placeholders_and_unlinked_tokens():
    gaps = rendered_link_gaps(
        '<p>cid:missing https://example.org/path help@example.org</p>'
        '<img src="https://example.org/pixel" />'
    )
    assert gaps == {
        "raw_cid_text": 1,
        "bare_web_text": 1,
        "bare_email_text": 1,
        "external_image_sources": 1,
        "clickable_links": 0,
    }


def test_private_rendering_gap_review_keeps_normal_links_out_of_report(repository):
    from email_reader.contracts import MessagePayload
    from email_reader.diagnostics import rendering_gap_review_text

    repository.ingest_message("account-1", MessagePayload(
        provider_id="render-audit", provider_thread_id="render-audit",
        subject="Fictional link", body_text="Read https://example.org/report or ask help@example.org.",
    ))
    value = rendering_gap_review_text(repository.db)
    assert "No unlinked web or email tokens detected" in value
    assert "example.org" not in value
