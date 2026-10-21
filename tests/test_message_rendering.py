from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from sqlite3 import connect

from bs4 import BeautifulSoup

from email_reader.contracts import MessagePayload
from email_reader.database import Database
from email_reader.image_types import is_mail_image, mail_image_sql, visible_mail_file_sql
from email_reader.providers.mime import parse_rfc822
from email_reader.rendering import (
    email_document,
    fit_message_images,
    highlight_key_details,
    preview_snippet,
    render_message,
    render_source_excerpt,
    safe_link,
)
from email_reader.repository import MailRepository


def test_descriptive_links_and_plain_url_labels():
    rendered = render_message(
        "", '<a href="https://example.com/very/long?secret=123">Review the quote</a>', []
    )
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert soup.a.text == "Review the quote"
    assert soup.a["href"] == "https://example.com/very/long?secret=123"
    rendered = render_message(
        "Visit www.example.com<https://www.example.com/long?x=1&y=2>. "
        "Email team@example.com or mailto:info@example.net. "
        "(https://example.com/wiki/Topic_(detail)).",
        "",
        [],
    )
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert [a["href"] for a in soup.find_all("a")] == [
        "https://www.example.com/long?x=1&y=2",
        "mailto:team@example.com",
        "mailto:info@example.net",
        "https://example.com/wiki/Topic_(detail)",
    ]
    assert soup.get_text().count("www.example.com") == 1
    assert soup.get_text().endswith("(example.com).")


def test_document_declarations_never_become_visible_text_or_links():
    markup = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
        '<html><head><title>Transport title</title></head><body>'
        '<p>Your documents are attached.</p>'
        '<a href="https://example.org/documents">Review documents</a>'
        '<!-- hidden https://example.net/tracker -->'
        '<?transport https://example.net/metadata?>'
        '</body></html>'
    )
    soup = BeautifulSoup(render_message("", markup, []).html, "html.parser")
    assert soup.get_text() == "Your documents are attached.Review documents"
    assert [(link.text, link["href"]) for link in soup.find_all("a")] == [
        ("Review documents", "https://example.org/documents")
    ]
    assert "DOCTYPE" not in str(soup)
    assert "w3.org" not in str(soup)
    assert "encoding" not in str(soup)


def test_escaped_declaration_examples_remain_sender_visible_text():
    markup = '<p>Example: &lt;!DOCTYPE html&gt; and &lt;?xml version="1.0"?&gt;</p>'
    soup = BeautifulSoup(render_message("", markup, []).html, "html.parser")
    assert soup.get_text() == 'Example: <!DOCTYPE html> and <?xml version="1.0"?>'


def test_document_headers_do_not_hide_plain_alternative_or_inline_images():
    markup = '<?xml version="1.0"?><!DOCTYPE html><html><body></body></html>'
    soup = BeautifulSoup(render_message("Plain alternative", markup, []).html, "html.parser")
    assert soup.get_text() == "Plain alternative"
    attachment = {
        "id": "picture", "mime_type": "image/png", "content_id": "logo",
        "content_digest": "available-original",
    }
    result = render_message("", markup.replace('</body>', '<img src="cid:logo" /></body>'), [attachment])
    assert result.embedded_ids == {"picture"}
    assert 'image://mail/picture' in result.html
    assert "DOCTYPE" not in result.html


def test_outlook_plain_text_link_pairs_render_once_with_original_targets():
    body = (
        "Write team@example.com<mailto:team@example.com> or "
        "help@example.net <mailto:help@example.net>. "
        "Review [Your policy](https://files.example.org/private?id=4) and "
        "[Insurance portal]<https://portal.example.org/long/path?token=5>."
    )
    soup = BeautifulSoup(render_message(body, "", []).html, "html.parser")
    assert [(link.text, link["href"]) for link in soup.find_all("a")] == [
        ("team@example.com", "mailto:team@example.com"),
        ("help@example.net", "mailto:help@example.net"),
        ("Your policy", "https://files.example.org/private?id=4"),
        ("Insurance portal", "https://portal.example.org/long/path?token=5"),
    ]
    assert soup.get_text().count("team@example.com") == 1
    assert soup.get_text().count("help@example.net") == 1
    assert preview_snippet(body) == (
        "Write team@example.com or help@example.net. "
        "Review Your policy and Insurance portal."
    )


def test_internationalized_email_addresses_are_clickable_without_duplicate_text():
    body = (
        "Contact 名字@例子.公司 or 客服@保险.中国<mailto:客服@保险.中国> "
        "or support@example.xn--fiqs8s."
    )
    soup = BeautifulSoup(render_message(body, "", []).html, "html.parser")
    assert [(link.text, link["href"]) for link in soup.find_all("a")] == [
        ("名字@例子.公司", "mailto:名字@例子.公司"),
        ("客服@保险.中国", "mailto:客服@保险.中国"),
        ("support@example.xn--fiqs8s", "mailto:support@example.xn--fiqs8s"),
    ]
    assert soup.get_text() == "Contact 名字@例子.公司 or 客服@保险.中国 or support@example.xn--fiqs8s."
    assert preview_snippet(body) == soup.get_text()

    split_html = '<p>Contact <span>客服</span><font>@保险.中国</font> today.</p>'
    split = BeautifulSoup(render_message("", split_html, []).html, "html.parser")
    assert [(link.text, link["href"]) for link in split.find_all("a")] == [
        ("客服@保险.中国", "mailto:客服@保险.中国"),
    ]
    assert split.get_text() == "Contact 客服@保险.中国 today."


def test_outlook_plain_link_pairs_never_make_unsafe_targets_clickable():
    body = "[Do not click]<javascript:alert(1)> [Details]<https://example.org/safe>"
    soup = BeautifulSoup(render_message(body, "", []).html, "html.parser")
    assert [(link.text, link["href"]) for link in soup.find_all("a")] == [
        ("Details", "https://example.org/safe"),
    ]
    assert "javascript:alert(1)" in soup.get_text()


def test_broken_html_anchor_does_not_block_safe_links_in_its_text():
    markup = (
        '<p><a href="#">help@example.org</a> '
        '<a href="javascript:alert(1)">https://example.org/very/long/path</a></p>'
    )
    soup = BeautifulSoup(render_message("", markup, []).html, "html.parser")
    assert [(link.get_text(), link["href"]) for link in soup.find_all("a")] == [
        ("help@example.org", "mailto:help@example.org"),
        ("example.org", "https://example.org/very/long/path"),
    ]
    assert "javascript:" not in str(soup)


def test_split_html_link_text_is_relinked_without_disturbing_inline_image():
    markup = (
        '<p>Add <span>cvspharmacy</span><font>@alerts.cvs.com</font> to contacts. '
        'Read <span>https://example.org/very/</span><span>long/path</span>. '
        '<img src="cid:logo@example.org" /></p>'
    )
    rendered = render_message("", markup, [{
        "id": "logo", "provider_id": "logo", "content_id": "logo@example.org",
        "filename": "logo.png", "mime_type": "image/png", "content_digest": "digest",
    }])
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert [(link.get_text(), link["href"]) for link in soup.find_all("a")] == [
        ("cvspharmacy@alerts.cvs.com", "mailto:cvspharmacy@alerts.cvs.com"),
        ("example.org", "https://example.org/very/long/path"),
    ]
    assert soup.img["src"] == "image://mail/logo"
    assert rendered.embedded_ids == {"logo"}


def test_raw_url_closing_brackets_stay_outside_click_target():
    body = (
        "[https://example.org/report] {https://example.net/quote} "
        "https://example.com/search?q=[draft] "
        "https://example.com/view?selection={current}"
    )
    soup = BeautifulSoup(render_message(body, "", []).html, "html.parser")
    assert [(link.text, link["href"]) for link in soup.find_all("a")] == [
        ("example.org", "https://example.org/report"),
        ("example.net", "https://example.net/quote"),
        ("example.com", "https://example.com/search?q=[draft]"),
        ("example.com", "https://example.com/view?selection={current}"),
    ]
    assert "[example.org]" in soup.get_text()
    assert "{example.net}" in soup.get_text()


def test_empty_html_alternative_never_hides_written_plain_text():
    rendered = render_message(
        "This is the reviewed draft body.", "<div><br /></div><div></div>", []
    )
    assert BeautifulSoup(rendered.html, "html.parser").get_text() == "This is the reviewed draft body."


def test_image_only_html_alternative_keeps_plain_words_and_embedded_picture():
    image = {"id": "logo", "content_id": "logo@example.net", "mime_type": "image/png",
             "content_digest": "digest"}
    rendered = render_message(
        "Here is the logo.", '<div><img src="cid:logo@example.net" /></div>', [image]
    )
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert "Here is the logo." in soup.get_text()
    assert [item["src"] for item in soup.find_all("img")] == ["image://mail/logo"]
    assert rendered.embedded_ids == {"logo"}

    # If plain text already positions the same image, never show it twice.
    positioned = render_message(
        "Before [cid:logo@example.net] after", '<img src="cid:logo@example.net" />', [image]
    )
    assert positioned.html.count('src="image://mail/logo"') == 1
    assert positioned.html.index("Before") < positioned.html.index("image://mail/logo") < positioned.html.index("after")

    second = {"id": "diagram", "content_id": "diagram@example.net", "mime_type": "image/png",
              "content_digest": "digest-2"}
    mixed = render_message(
        "Before [cid:logo@example.net] after",
        '<img src="cid:logo@example.net" /><img src="cid:diagram@example.net" />',
        [image, second],
    )
    assert mixed.html.count('src="image://mail/logo"') == 1
    assert mixed.html.count('src="image://mail/diagram"') == 1
    assert mixed.embedded_ids == {"logo", "diagram"}


def test_image_only_html_with_invisible_filler_keeps_plain_words():
    image = {
        "id": "banner",
        "content_id": "banner@example.net",
        "mime_type": "image/png",
        "content_digest": "digest",
    }
    rendered = render_message(
        "The meaningful message text",
        '<div>\u200b\ufeff<img src="cid:banner@example.net" alt="Header" /></div>',
        [image],
    )
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert "The meaningful message text" in soup.get_text()
    assert soup.find("img")["src"] == "image://mail/banner"


def test_image_heavy_html_keeps_meaningful_plain_alternative():
    image = {
        "id": "banner",
        "content_id": "banner@example.net",
        "mime_type": "image/png",
        "content_digest": "digest",
    }
    rendered = render_message(
        "We received your application and will contact you soon.",
        '<div style="font-size:1px;color:#ffffff">Preview</div>'
        '<img src="cid:banner@example.net" alt="Header" />',
        [image],
    )
    assert "We received your application" in BeautifulSoup(
        rendered.html, "html.parser"
    ).get_text()


def test_hidden_preheader_does_not_suppress_plain_fallback():
    image = {
        "id": "banner",
        "content_id": "banner@example.net",
        "mime_type": "image/png",
        "content_digest": "digest",
    }
    rendered = render_message(
        "We received your application and will contact you soon.",
        '<div style="display:none;max-height:0;overflow:hidden">'
        "We received your application and will contact you soon.</div>"
        '<img src="cid:banner@example.net" alt="Header" />',
        [image],
    )
    text = BeautifulSoup(rendered.html, "html.parser").get_text(" ", strip=True)
    assert "We received your application" in text
    assert "display:none" not in rendered.html


def test_image_only_remote_html_keeps_plain_words_without_loading_remote_picture():
    rendered = render_message(
        "Please see the picture.", '<img src="https://images.example.net/pixel" alt="Policy photo" />', []
    )
    assert "Please see the picture." in BeautifulSoup(rendered.html, "html.parser").get_text()
    assert "Policy photo" in rendered.html
    assert not BeautifulSoup(rendered.html, "html.parser").find("img")
    assert rendered.remote_urls == {"https://images.example.net/pixel"}


def test_preview_hides_tracking_urls_and_cid_placeholders_without_changing_mail():
    preview = preview_snippet(
        "Your quote is waiting https://click.example.com/very/long?private=abc. "
        "See [Review the quote](https://example.org/private). "
        "www.example.net<https://redirect.example.net/secret> [cid:picture1]"
    )
    assert preview == (
        "Your quote is waiting click.example.com. See Review the quote. "
        "redirect.example.net"
    )
    assert preview_snippet("<mailto:team@example.org> cid:image001.png@abc") == "team@example.org"
    assert preview_snippet("Agent www.instagram.com <www.lin") == "Agent www.instagram.com"
    assert preview_snippet(
        "Write tkuch@allstate.com<mailto:tkuch@allstate.com> [Allstate]<http://l.quote"
    ) == "Write tkuch@allstate.com [Allstate]"
    assert preview_snippet(
        'Details <a href="https://example.net target="_blank">open document</a> today'
    ) == "Details open document today"
    assert preview_snippet(
        "Alert [_Click here_ to view the email in the browser]("
    ) == "Alert Click here to view the email in the browser"
    assert preview_snippet(
        "Open [policy document](https://files.example.org/long/trunc"
    ) == "Open policy document"


def test_preview_hides_repeated_mojibake_zero_width_filler():
    legacy = "CVS " + ("â\x80\x8c \xa0" * 18) + "Updated terms"
    assert preview_snippet(legacy) == "CVS Updated terms"
    assert preview_snippet("CVS " + ("â\x80\x8c \xa0" * 18) + "â") == "CVS"
    assert preview_snippet("A developer 👩\u200d💻 wrote back") == "A developer 👩\u200d💻 wrote back"


def test_image_only_links_have_readable_click_targets_without_hiding_images():
    attachments = [{"id": "tiny", "content_id": "social@example.net", "mime_type": "image/jpeg",
                    "content_digest": "digest"}]
    markup = (
        '<a href="https://social.example.net/long/tracked/url">'
        '<img src="cid:social@example.net" /></a>'
        '<a href="https://agent.example.org/contact">'
        '<img src="cid:social@example.net" alt="Contact the agent" /></a>'
    )
    rendered = render_message("", markup, attachments)
    links = BeautifulSoup(rendered.html, "html.parser").find_all("a")
    assert [(item.get_text().strip(), item["href"]) for item in links] == [
        ("social.example.net", "https://social.example.net/long/tracked/url"),
        ("Contact the agent", "https://agent.example.org/contact"),
    ]
    assert all(item.img and item.img["src"] == "image://mail/tiny" for item in links)
    assert rendered.embedded_ids == {"tiny"}


def test_raw_url_labels_and_standalone_angle_links_show_short_actual_destination():
    html_message = (
        '<a href="https://files.example.net/long/download?token=123">'
        'https://files.example.net/long/download</a>'
        '<a href="https://redirect.example.org/click?id=7">www.example.com</a>'
    )
    soup = BeautifulSoup(render_message("", html_message, []).html, "html.parser")
    assert [(a.text, a["href"]) for a in soup.find_all("a")] == [
        ("files.example.net", "https://files.example.net/long/download?token=123"),
        ("redirect.example.org", "https://redirect.example.org/click?id=7"),
    ]
    plain = BeautifulSoup(
        render_message("Open <https://example.com/very/long?key=1> or <mailto:help@example.net>", "", []).html,
        "html.parser",
    )
    assert [(a.text, a["href"]) for a in plain.find_all("a")] == [
        ("example.com", "https://example.com/very/long?key=1"),
        ("help@example.net", "mailto:help@example.net"),
    ]
    assert "&lt;" not in str(plain) and "&gt;" not in str(plain)


def test_surrounding_quotes_do_not_change_link_destinations():
    body = ("To: 'owner@example.com' <owner@example.com>\n"
            "Email o'brien@example.net or 'o'brien@example.net'.\n"
            '\"https://example.com/a/long/path\" and ‘https://example.net/other’.')
    result = render_message(body, "", [])
    soup = BeautifulSoup(result.html, "html.parser")
    assert [a["href"] for a in soup.find_all("a")] == [
        "mailto:owner@example.com", "mailto:owner@example.com",
        "mailto:o'brien@example.net", "mailto:o'brien@example.net",
        "https://example.com/a/long/path", "https://example.net/other",
    ]
    assert "'owner@example.com'" in soup.get_text()
    assert '\"example.com\"' in soup.get_text() and '‘example.net’.' in soup.get_text()


def test_markdown_links_preserve_labels_and_balanced_url_parentheses():
    value = ("[Orientation Schedule] (https://canvas.example.net/courses/1/files/2) "
             "[Topic](https://example.com/Topic_(detail))[Email](mailto:office@example.net) "
             '[<script>bad()</script>](https://example.net/safe)')
    rendered = render_message(value, "", [])
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert [(a.text, a["href"]) for a in soup.find_all("a")] == [
        ("Orientation Schedule", "https://canvas.example.net/courses/1/files/2"),
        ("Topic", "https://example.com/Topic_(detail)"),
        ("Email", "mailto:office@example.net"),
        ("<script>bad()</script>", "https://example.net/safe"),
    ]
    assert soup.script is None and soup.img is None


def test_chat_text_never_interprets_ai_supplied_html():
    from PySide6.QtCore import QObject

    from email_reader.controller import AppController

    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    rendered = controller.assistantTextHtml('<img src="https://tracker.example/pixel"><script>bad()</script> [Details](https://example.net/file)')
    soup = BeautifulSoup(rendered, "html.parser")
    assert soup.img is None and soup.script is None
    assert any(a.text == "Details" and a["href"] == "https://example.net/file" for a in soup.find_all("a"))


def test_related_source_excerpt_renders_mail_html_passively_and_keeps_links():
    excerpt = (
        "<p>Dear reader,<br />Review the "
        "<a href='https://example.net/documents/long/path'>https://example.net/documents/long/path</a>."
        "<img src='https://tracker.example.net/pixel' alt='signature' />"
        "<script>alert('bad')</script></p>"
    )
    result = render_source_excerpt(excerpt)
    soup = BeautifulSoup(result, "html.parser")
    assert "Dear reader," in soup.get_text()
    assert soup.br is not None and "<br" not in soup.get_text()
    assert [(item.text, item["href"]) for item in soup.find_all("a")] == [
        ("example.net", "https://example.net/documents/long/path"),
    ]
    assert soup.img is None and soup.script is None
    plain = BeautifulSoup(render_source_excerpt("Quote <https://example.org/long/path>"), "html.parser")
    assert [(item.text, item["href"]) for item in plain.find_all("a")] == [
        ("example.org", "https://example.org/long/path"),
    ]


def test_protocol_relative_links_and_images_stay_passive_until_requested():
    markup = '<a href="//example.com/long/path">Document details</a><img src="//images.example.com/logo.png" alt="Logo" />'
    result = render_message("", markup, [])
    soup = BeautifulSoup(result.html, "html.parser")
    assert soup.a["href"] == "https://example.com/long/path" and soup.a.text == "Document details"
    assert soup.img is None and result.remote_urls == {"https://images.example.com/logo.png"}
    loaded = render_message("", markup, [], remote_images={"https://images.example.com/logo.png": "verified-image"})
    assert 'image://mail/verified-image' in loaded.html
    assert not safe_link("https://example.com:invalid/path")


def test_invisible_template_filler_does_not_fill_reading_pane():
    markup = (
        "<p>Open to learn whatâ\x80\x99s changed.</p>"
        + "<div>â\x80\x8c</div>" * 150
        + '<p><a href="https://example.net/terms?tracking=long">Review terms</a></p>'
    )
    rendered = render_message("", markup, [])
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert "what’s changed" in soup.get_text()
    assert soup.a.text == "Review terms"
    assert soup.a["href"] == "https://example.net/terms?tracking=long"
    assert "â\x80" not in rendered.html
    assert len(rendered.html) < 400


def test_all_common_cid_placeholders_resolve_without_email_autolinking():
    attachments = [{"id": "logo", "content_id": "logo@example.net", "mime_type": "image/png", "content_digest": "digest"}]
    for placeholder in ["[cid:logo@example.net]", "<cid:logo@example.net>", "cid:logo@example.net"]:
        result = render_message(f"Before {placeholder} After", "", attachments)
        assert result.embedded_ids == {"logo"}
        assert 'image://mail/logo' in result.html and 'cid:' not in result.html
        assert 'mailto:' not in result.html


def test_inline_non_image_document_stays_in_main_attachment_list():
    from types import SimpleNamespace

    from PySide6.QtCore import QObject

    from email_reader.controller import AppController

    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.mail_images = SimpleNamespace(register=lambda _items: None)
    controller._message_remote_images = {}
    message = {
        "id": "message-1",
        "body_text": "Please review [cid:logo@example.net] and the PDF.",
        "body_html": "",
        "new_content": "Please review [cid:logo@example.net] and the PDF.",
        "is_draft": False,
        "attachments": [
            {"id": "logo", "filename": "signature.png", "content_id": "logo@example.net",
             "mime_type": "image/png", "content_digest": "digest", "is_inline": True},
            {"id": "document", "filename": "policy.pdf", "mime_type": "application/pdf",
             "content_digest": "pdf-digest", "is_inline": True},
        ],
    }
    controller._render_message(message)
    assert [item["id"] for item in message["file_attachments"]] == ["document"]
    assert [item["id"] for item in message["inline_attachments"]] == ["logo"]
    assert 'src="image://mail/logo"' in message["display_html"]


def test_empty_new_content_falls_back_to_saved_message_text():
    from types import SimpleNamespace

    from PySide6.QtCore import QObject

    from email_reader.controller import AppController

    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.mail_images = SimpleNamespace(register=lambda _items: None)
    controller._message_remote_images = {}
    message = {
        "id": "message-empty-new-content",
        "body_text": "We received your application and will contact you soon.",
        "body_html": '<img src="cid:banner" alt="Email header" />',
        "new_content": "\u200b\ufeff",
        "snippet": "Short provider preview",
        "is_draft": False,
        "attachments": [{
            "id": "banner",
            "filename": "header.png",
            "content_id": "banner",
            "mime_type": "image/png",
            "content_digest": "digest",
            "is_inline": True,
        }],
    }

    controller._render_message(message)

    assert "We received your application" in BeautifulSoup(
        message["display_html"], "html.parser"
    ).get_text()

    message["body_text"] = ""
    message["new_content"] = "\u200b"
    controller._render_message(message)
    assert "Short provider preview" in BeautifulSoup(
        message["display_html"], "html.parser"
    ).get_text()


def test_sql_inline_image_filter_matches_mail_image_classifier():
    samples = [
        ("signature.jpg", "image/jpeg"),
        ("signature.png", "application/octet-stream"),
        ("diagram.svg", "image/svg+xml; charset=utf-8"),
        ("policy.pdf", "application/pdf"),
        ("scan.tif", ""),
    ]
    connection = connect(":memory:")
    try:
        for filename, mime_type in samples:
            image, visible = connection.execute(
                f"SELECT {mail_image_sql('a')}, {visible_mail_file_sql('a')} "
                "FROM (SELECT ? AS filename, ? AS mime_type, 1 AS is_inline) a",
                (filename, mime_type),
            ).fetchone()
            assert bool(image) == is_mail_image(mime_type, filename)
            assert bool(visible) != bool(image)
    finally:
        connection.close()


def test_inline_image_fits_pane_without_redecrypting_original_on_resize(repository):
    from unittest.mock import Mock

    from PySide6.QtCore import QBuffer, QIODevice, QSize
    from PySide6.QtGui import QImage

    from email_reader.mail_images import MailImageProvider

    source = QImage(1200, 200, QImage.Format_RGB32)
    source.fill("#174EA6")
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert source.save(buffer, "PNG")
    original = bytes(buffer.data())
    digest, _ = repository.blobs.put(original)
    provider = MailImageProvider(repository.blobs)
    provider.register([{"id": "image", "mime_type": "image/png", "content_digest": digest}])
    repository.blobs.get = Mock(wraps=repository.blobs.get)
    size = QSize()
    first = provider.requestImage("image?width=300", size, QSize())
    assert first.width() == 300 and 49 <= first.height() <= 50
    second = provider.requestImage("image?width=180", size, QSize())
    assert second.width() == 180 and 29 <= second.height() <= 30
    assert repository.blobs.get.call_count == 1
    assert repository.blobs.get(digest) == original, "Display sizing must not alter the original"
    provider.remote["remote"] = source
    remote = provider.requestImage("remote?width=220", size, QSize())
    assert remote.width() == 220
    markup = '<p>Before<img src="image://mail/image" />After</p>'
    fitted = fit_message_images(markup, 302)
    assert 'image://mail/image?width=300"' in fitted
    assert 'width=178"' in fit_message_images(fitted, 180)
    assert 'width=1"' in fit_message_images(markup, 0)
    assert 'width=560"' in fit_message_images(markup, 5000)


def test_passive_renderer_cannot_load_local_or_remote_resources():
    unsafe = """<base href="file:///private/"><script>secret()</script><style>body{background:url(https://tracker)}</style>
    <form><input value="secret"></form><img src="file:///private/key"><img src="data:image/svg+xml,bad">
    <iframe src="https://tracker"></iframe><a href="javascript:bad()">Readable</a>
    <div style="background-image:url(https://tracker)" onclick="bad()">Safe</div>
    <img src="https://images.example.com/pixel" alt="Logo"><a href="file:///private">Local</a>"""
    result = render_message("", unsafe, [])
    assert not any(
        word in result.html
        for word in ["secret", "file:", "data:", "javascript:", "onclick", "tracker"]
    )
    assert not BeautifulSoup(result.html, "html.parser").find("img")
    assert result.remote_urls == {"https://images.example.com/pixel"}
    assert "Readable" in result.html and "Safe" in result.html and "Logo" in result.html
    for value in ["file:///x", "javascript:alert(1)", "https://[invalid", "https://example.com\n"]:
        # Whitespace around a valid link is harmless; embedded controls are not.
        if not value.endswith("\n"):
            assert not safe_link(value)
    assert not safe_link("https://exam\nple.com")
    assert not safe_link("https://exam\x7fple.com")


def test_cid_matching_and_position_in_both_html_and_plain_text():
    attachments = [
        {
            "id": "image-id",
            "content_id": "image001.png@sender",
            "provider_id": "opaque",
            "filename": "image001.png",
            "mime_type": "image/png",
            "content_digest": "digest",
        }
    ]
    for text, html in [
        ("Before [cid:image001.png@sender] After", ""),
        ("", '<p>Before<img src="cid:image001.png%40sender" />After</p>'),
    ]:
        result = render_message(text, html, attachments)
        assert (
            result.html.index("Before")
            < result.html.index("image://mail/image-id")
            < result.html.index("After")
        )
        assert "cid:" not in result.html
        assert result.embedded_ids == {"image-id"}


def test_nested_one_column_layout_tables_use_full_reading_width_without_losing_inline_order():
    attachments = [{"id": "logo", "content_id": "logo@sender", "mime_type": "image/png",
                    "content_digest": "digest"}]
    markup = (
        "html\n\n<table><tbody><tr><td><table><tr><td>"
        '<p>Before <img src="cid:logo@sender" alt="logo" /> then '
        '<a href="https://example.com/details">details</a> after.</p>'
        "</td></tr></table></td></tr></tbody></table>"
    )
    rendered = render_message("", markup, attachments)
    soup = BeautifulSoup(rendered.html, "html.parser")
    assert not soup.find("table"), "Layout wrappers should not squeeze text into a narrow cell"
    assert not soup.get_text().lstrip().startswith("html")
    assert rendered.embedded_ids == {"logo"}
    assert rendered.html.index("Before") < rendered.html.index("image://mail/logo")
    assert rendered.html.index("image://mail/logo") < rendered.html.index("details")
    assert soup.a["href"] == "https://example.com/details"


def test_multicolumn_data_table_keeps_its_columns():
    markup = "<table><tr><th>Date</th><th>Amount</th></tr><tr><td>Oct 1</td><td>$20</td></tr></table>"
    soup = BeautifulSoup(render_message("", markup, []).html, "html.parser")
    assert soup.table is not None
    assert len(soup.find_all("tr")) == 2
    assert [cell.get_text() for cell in soup.find_all("td")] == ["Oct 1", "$20"]
    assert "cid:" not in render_message("[cid:missing]", "", []).html


def test_html_subtype_marker_before_comment_is_not_displayed():
    markup = "html\n\n<!-- Mail metadata --><div>Dear reader</div>"
    rendered = render_message("", markup, [])
    assert "html" not in BeautifulSoup(rendered.html, "html.parser").get_text().lower()
    assert "Dear reader" in rendered.html


def test_nested_html_subtype_marker_is_not_displayed():
    markup = "<div><span>html</span><div>Dear reader</div></div>"
    rendered = render_message("Dear reader", markup, [])
    assert BeautifulSoup(rendered.html, "html.parser").get_text() == "Dear reader"


def test_genuine_leading_html_word_is_preserved():
    markup = "<div>html<br/>is a markup language</div>"
    rendered = render_message("html is a markup language", markup, [])
    assert BeautifulSoup(rendered.html, "html.parser").get_text().startswith("html")


def test_mime_inline_image_without_filename_is_not_lost():
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message.set_content("Picture")
    message.add_alternative('<p>Picture<img src="cid:logo" /></p>', subtype="html")
    message.get_payload()[1].add_related(
        b"image payload", maintype="image", subtype="png", cid="<logo>"
    )
    parsed = parse_rfc822(message.as_bytes(), provider_id="m", provider_thread_id="t")
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content_id == "logo"
    assert parsed.attachments[0].is_inline
    assert parsed.attachments[0].content == b"image payload"


def test_generic_binary_cid_picture_is_recovered_and_displayable(repository):
    from PySide6.QtCore import QBuffer, QIODevice, QSize
    from PySide6.QtGui import QImage

    from email_reader.mail_images import MailImageProvider

    picture = QImage(16, 12, QImage.Format_RGB32)
    picture.fill("#174EA6")
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert picture.save(buffer, "PNG")
    original = bytes(buffer.data())

    mail = EmailMessage()
    mail.set_content("Before [cid:picture@example.net] after")
    mail.add_attachment(
        original, maintype="application", subtype="octet-stream",
        cid="<picture@example.net>",
    )
    parsed = parse_rfc822(mail.as_bytes(), provider_id="m", provider_thread_id="t")
    assert len(parsed.attachments) == 1
    attachment = parsed.attachments[0]
    assert attachment.mime_type == "image/png"
    assert attachment.is_inline
    digest, _ = repository.blobs.put(attachment.content)
    rendered = render_message(parsed.body_text, parsed.body_html, [{
        "id": "picture", "content_id": attachment.content_id,
        "filename": attachment.filename, "mime_type": attachment.mime_type,
        "content_digest": digest,
    }])
    assert rendered.embedded_ids == {"picture"}
    assert rendered.html.count('src="image://mail/picture"') == 1
    assert rendered.html.index("Before") < rendered.html.index("image://mail/picture") < rendered.html.index("after")
    provider = MailImageProvider(repository.blobs)
    provider.register([{"id": "picture", "content_digest": digest,
                       "filename": attachment.filename, "mime_type": attachment.mime_type}])
    assert provider.requestImage("picture", QSize(), QSize()).size() == QSize(16, 12)


def test_generic_mime_raster_filename_is_allowed_but_contradictory_type_is_not():
    assert is_mail_image("application/octet-stream", "Picture (Device Independent Bitmap) 7.jpg")
    assert is_mail_image("binary/octet-stream", "logo.png")
    assert not is_mail_image("application/pdf", "logo.png")
    assert not is_mail_image("application/octet-stream", "script.svg")
    assert not is_mail_image("image/svg+xml", "script.svg")


def test_content_location_picture_is_imported_and_resolved():
    message = EmailMessage()
    message.set_content("Picture")
    message.add_alternative('<p>Before<img src="assets/map.png" alt="Map" />after</p>', subtype="html")
    message.get_payload()[1].add_related(b"png bytes", maintype="image", subtype="png")
    related = message.get_payload()[1].get_payload()[-1]
    related["Content-Location"] = "assets/map.png"
    parsed = parse_rfc822(message.as_bytes(), provider_id="m", provider_thread_id="t")
    assert parsed.attachments[0].content_location == "assets/map.png"
    rendered = render_message(parsed.body_text, parsed.body_html, [{
        "id": "map", "content_location": "assets/map.png", "mime_type": "image/png",
        "filename": "map.png", "content_digest": "digest",
    }])
    assert rendered.embedded_ids == {"map"}
    assert 'src="image://mail/map"' in rendered.html


def test_safe_sender_layout_and_sandboxed_document_keep_visual_hierarchy():
    rendered = render_message(
        "",
        '<h2 style="color:#123456;position:fixed">Heading</h2>'
        '<table style="border-collapse:collapse"><tr><th colspan="2">Details</th></tr>'
        '<tr><td><a href="https://example.org/map"><button>View map</button></a></td>'
        '<td style="background:url(https://tracker.example/pixel)">Room 4</td></tr></table>',
        [],
    )
    assert "position:fixed" not in rendered.html
    assert "tracker.example" not in rendered.html
    assert "color:#123456" in rendered.html
    assert 'colspan="2"' in rendered.html
    assert 'class="sender-button"' in rendered.html
    document = email_document(rendered.html, reading_mode=True)
    assert "default-src 'none'" in document
    assert "overflow-x: auto" in document
    assert "text-decoration: underline" in document


def test_key_detail_highlights_are_exact_and_only_enabled_by_caller():
    fragment = "<p>Payment of $1,250.00 is due by October 12, 2026 at 4:30 PM.</p>"
    highlighted = BeautifulSoup(highlight_key_details(fragment), "html.parser")
    values = [item.get_text() for item in highlighted.select("mark.key-detail")]
    assert "$1,250.00" in values
    assert "due by October 12, 2026" in values
    assert "4:30 PM" in values
    assert highlighted.get_text() == "Payment of $1,250.00 is due by October 12, 2026 at 4:30 PM."


def test_local_read_override_survives_refresh_and_new_mail_is_unread(repository):
    payload = MessagePayload(
        provider_id="m",
        provider_thread_id="t",
        is_unread=True,
        body_text="Hello",
        labels=["INBOX", "UNREAD"],
    )
    repository.ingest_message("account-1", payload)
    thread = repository.list_threads()[0]["id"]
    assert repository.unread_counts()["inbox"] == 1
    repository.set_thread_unread(thread, False)
    assert not repository.list_threads()[0]["is_unread"]
    assert repository.unread_counts()["inbox"] == 0
    repository.ingest_message("account-1", payload)
    assert not repository.list_threads()[0]["is_unread"]
    # Provider flag remains unread; the local override never changes its labels.
    assert repository.db.scalar("SELECT is_unread FROM messages") == 1
    assert "UNREAD" in repository.get_thread(thread)["messages"][0]["labels"]
    payload.provider_id = "new"
    payload.sent_at = datetime.now(UTC) + timedelta(seconds=1)
    repository.ingest_message("account-1", payload)
    assert repository.list_threads()[0]["is_unread"]
    assert repository.unread_counts()["inbox"] == 1
    repository.set_thread_unread(thread, False)
    repository.set_thread_unread(thread, True)
    assert repository.get_thread(thread)["is_unread"] == 1


def test_local_read_persists_database_restart(repository, tmp_path):
    repository.ingest_message(
        "account-1", MessagePayload(provider_id="m", provider_thread_id="t", is_unread=True)
    )
    thread_id = repository.list_threads()[0]["id"]
    repository.set_thread_unread(thread_id, False)
    path = tmp_path / "fictional.db"
    repository.db.backup_to(path)
    reopened = Database(path, "test", allow_plaintext=True)
    reopened.open()
    restored = MailRepository(reopened, repository.blobs)
    assert restored.get_thread(thread_id)["is_unread"] == 0
    assert restored.unread_counts()["inbox"] == 0
    reopened.close()


def test_malformed_html_is_repaired_on_ingest_not_every_launch(repository):
    payload = MessagePayload(
        provider_id="bad", provider_thread_id="t", body_text="<html><body>Readable</body></html>"
    )
    repository.ingest_message("account-1", payload)
    message = repository.get_thread(repository.list_threads()[0]["id"])["messages"][0]
    assert message["body_text"] == "Readable"
    assert "<html>" in message["body_html"]
    assert repository.repair_malformed_html_bodies() == 0


def test_existing_table_fragment_is_repaired_without_provider_download(repository):
    markup = '<table><tr><td><p>Appointment details</p></td></tr></table>'
    identifier = repository.ingest_message(
        "account-1", MessagePayload(provider_id="table", provider_thread_id="table", body_text="Plain")
    )
    repository.db.execute(
        "UPDATE messages SET body_text=?,body_html='',snippet=? WHERE id=?",
        (markup, markup[:240], identifier),
    )
    assert repository.repair_malformed_html_bodies() == 1
    thread = repository.get_thread(repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (identifier,)))
    message = thread["messages"][0]
    assert message["body_text"] == "Appointment details"
    assert message["body_html"] == markup
    assert "<table" not in message["snippet"]
    assert repository.repair_malformed_html_bodies() == 0
