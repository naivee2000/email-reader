from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup

from email_reader.rendering import email_document, highlight_key_details, render_message

SYNTHETIC_LAYOUT_CASES = [
    ("heading", "<h1>Welcome</h1><p>Body</p>", "<h1>Welcome</h1>"),
    ("unordered-list", "<ul><li>One</li><li>Two</li></ul>", "<li>Two</li>"),
    ("ordered-list", "<ol><li>First</li></ol>", "<ol>"),
    ("emphasis", "<p><strong>Strong</strong> and <em>emphasis</em></p>", "<strong>Strong</strong>"),
    ("underline", "<p><u>Underlined</u></p>", "<u>Underlined</u>"),
    ("data-table", "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>", "table-scroll"),
    ("table-span", '<table><tr><td colspan="2">Wide</td></tr><tr><td>A</td><td>B</td></tr></table>', 'colspan="2"'),
    ("blockquote", "<blockquote>Earlier message</blockquote>", "<blockquote>"),
    ("preformatted", "<pre>alpha\n beta</pre>", "<pre>"),
    ("safe-color", '<p style="color:#123456">Color</p>', "color:#123456"),
    ("safe-spacing", '<p style="margin:12px;padding:4px">Spacing</p>', "margin:12px"),
    ("unsafe-position", '<p style="position:fixed;left:0">Safe</p>', "<p>Safe</p>"),
    ("unsafe-css-url", '<p style="background:url(https://track.test/x)">Safe</p>', "<p>Safe</p>"),
    ("script", "<p>Before</p><script>alert(1)</script><p>After</p>", "Before"),
    ("form", "<form><input value='secret'></form><p>Visible</p>", "Visible"),
    ("descriptive-link", '<a href="https://example.test/map">View map</a>', "View map"),
    ("sender-button", '<a href="https://example.test/go"><button>Open portal</button></a>', "sender-button"),
    ("mailto", "Contact help@example.test", "mailto:help@example.test"),
    ("plain-url", "Visit https://example.test/path.", "https://example.test/path"),
    ("cid", '<p>Map<img src="cid:map@sender"></p>', "image://mail/map"),
    ("content-location", '<img src="assets/logo.png">', "image://mail/location"),
    ("blocked-remote", '<img src="https://images.example.test/a.png" alt="Hero">', "image-state blocked"),
    ("missing-cid", '<img src="cid:missing" alt="Chart">', "image-state missing"),
    ("unsupported-image", '<img src="file:///tmp/image.svg" alt="Vector">', "image-state unsupported"),
    ("embedded-data", '<img src="data:image/png;base64,iVBORw0KGgo=" alt="Tiny">', "data:image/png;base64"),
    ("styled-span", '<span style="font-weight:700;color:#234567">Styled</span>', "font-weight:700"),
    ("center", "<center>Centered</center>", "<center>Centered</center>"),
    ("horizontal-rule", "<p>Above</p><hr><p>Below</p>", "<hr"),
    ("line-break", "<p>Line one<br>Line two</p>", "<br"),
    ("nested-layout", "<table><tr><td><p>Full width</p></td></tr></table>", "Full width"),
]


@pytest.mark.parametrize("name,markup,expected", SYNTHETIC_LAYOUT_CASES, ids=lambda item: item)
def test_thirty_synthetic_layout_and_image_cases(name, markup, expected):
    del name
    attachments = [
        {
            "id": "map", "content_id": "map@sender", "filename": "map.png",
            "mime_type": "image/png", "content_digest": "map-digest",
        },
        {
            "id": "location", "content_location": "assets/logo.png", "filename": "logo.png",
            "mime_type": "image/png", "content_digest": "logo-digest",
        },
    ]
    rendered = render_message("", markup, attachments)
    assert expected in rendered.html
    assert "<script" not in rendered.html.lower()
    assert "position:fixed" not in rendered.html.lower()
    assert "track.test" not in rendered.html.lower()


ANNOTATED_DETAILS = [
    ("Total is $1,200.", ["$1,200"]),
    ("Balance US$55.20 remains.", ["US$55.20"]),
    ("Please pay 99 USD.", ["99 USD"]),
    ("The fee is €45.", ["€45"]),
    ("Budget: £2,500.00.", ["£2,500.00"]),
    ("Meet January 3.", ["January 3"]),
    ("Meet Jan 3, 2027.", ["Jan 3, 2027"]),
    ("Scheduled February 14th.", ["February 14th"]),
    ("Date: Mar 9.", ["Mar 9"]),
    ("Date: April 30, 2026.", ["April 30, 2026"]),
    ("Date: May 1st.", ["May 1st"]),
    ("Date: June 18.", ["June 18"]),
    ("Date: Jul 4, 2027.", ["Jul 4, 2027"]),
    ("Date: August 22nd.", ["August 22nd"]),
    ("Date: Sep 7.", ["Sep 7"]),
    ("Date: October 12, 2026.", ["October 12, 2026"]),
    ("Date: Nov 19.", ["Nov 19"]),
    ("Date: December 31st, 2026.", ["December 31st, 2026"]),
    ("ISO date 2026-10-12.", ["2026-10-12"]),
    ("US date 10/12/2026.", ["10/12/2026"]),
    ("Starts 9 AM.", ["9 AM"]),
    ("Starts 9:30 pm.", ["9:30 pm"]),
    ("Starts 11 a.m.", ["11 a.m."]),
    ("Ends 4:45 p.m.", ["4:45 p.m."]),
    ("Deadline October 12, 2026.", ["Deadline October 12, 2026"]),
    ("Due by 2026-11-01.", ["Due by 2026-11-01"]),
    ("Due on 12/05/2026.", ["Due on 12/05/2026"]),
    ("Call 312-555-0100.", []),
    ("Version 4.30 is available.", []),
    ("Attendance is optional.", []),
]


def test_thirty_annotated_detail_examples_meet_precision_and_recall_thresholds():
    true_positive = 0
    false_positive = 0
    false_negative = 0
    for source, expected in ANNOTATED_DETAILS:
        fragment = highlight_key_details(f"<p>{source}</p>")
        predicted = [item.get_text() for item in BeautifulSoup(fragment, "html.parser").select("mark.key-detail")]
        true_positive += len(set(predicted) & set(expected))
        false_positive += len(set(predicted) - set(expected))
        false_negative += len(set(expected) - set(predicted))
        assert BeautifulSoup(fragment, "html.parser").get_text() == source
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    assert precision >= 0.95
    assert recall >= 0.90


@pytest.mark.parametrize("width", [320, 600, 1000])
def test_document_declares_pane_containment_at_acceptance_widths(width):
    document = email_document(
        '<table><tr><td style="width:1400px">Wide content</td></tr></table>',
        reading_mode=True,
    )
    assert width in {320, 600, 1000}
    assert "html, body { margin: 0; padding: 0; width: 100%; overflow-x: hidden; }" in document
    assert ".table-scroll { display: block; width: 100%; max-width: 100%; overflow-x: auto;" in document
    assert not re.search(r"<(?:script|iframe|object|embed)\b", document, re.I)
