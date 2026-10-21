"""Small, passive rich-text email renderer: never pass sender HTML to Qt unchanged."""

from __future__ import annotations

import html
import re
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from bs4 import (
    BeautifulSoup,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    ProcessingInstruction,
    Tag,
)

from email_reader.image_types import is_mail_image
from email_reader.text import repair_common_utf8_mojibake

_INVISIBLE_FILLER = "\u200b\u200c\u200d\u2060\ufeff"
_INVISIBLE_TRANSLATION = str.maketrans("", "", _INVISIBLE_FILLER)
_NONCONTENT_NODES = (Comment, Declaration, Doctype, ProcessingInstruction)

_SOURCE_ID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_BRACKET_SOURCE = re.compile(r"\[(?:(?:SOURCE|ATTACHMENT):\s*)?(" + _SOURCE_ID + r")\]", re.I)
_NATIVE_SOURCE = re.compile(r"cite(" + _SOURCE_ID + r"(?:" + _SOURCE_ID + r")*)", re.I)


def _referenced_sources(value: str) -> list[str]:
    references = [(match.start(), [match.group(1)]) for match in _BRACKET_SOURCE.finditer(value)]
    references.extend((match.start(), match.group(1).split(""))
                      for match in _NATIVE_SOURCE.finditer(value))
    return [source for _, sources in sorted(references) for source in sources]


def ensure_answer_citations(text: str, citations: list[dict]) -> str:
    """Show an answer-level link when a sourced answer lacks inline markers.

    Source cards remain separate. This only links IDs already validated against
    the retrieved messages/files; it never promotes a model-supplied URL or ID.
    """
    if not text.strip():
        return text
    valid = list(dict.fromkeys(
        str(item.get("source_id", "")).lower()
        for item in citations
        if re.fullmatch(_SOURCE_ID, str(item.get("source_id", "")))
    ))
    if not valid or any(source.lower() in valid for source in _referenced_sources(text)):
        return text
    return text.rstrip() + " " + " ".join(f"[{source}]" for source in valid)


def summary_citations(summary: dict, thread: dict) -> list[dict]:
    """Expose only source IDs belonging to the summarized conversation.

    Summaries cite original message/attachment IDs, whereas search answers cite
    indexed chunk IDs. Keep the former intact so a cached summary still points
    to the exact original after search chunks are rebuilt.
    """
    valid_ids = {
        str(source["id"]).lower()
        for message in thread.get("messages", [])
        for source in (message, *message.get("attachments", []))
    }
    fields = (
        "key_points", "decisions", "dates_and_deadlines", "action_items",
        "important_attachments",
    )
    ordered: list[str] = []
    for section in fields:
        for bullet in summary.get(section, []):
            ordered.extend(_referenced_sources(bullet))
    citations: list[dict] = []
    seen: set[str] = set()
    for raw_id in ordered:
        source_id = str(raw_id).lower()
        if source_id in valid_ids and source_id not in seen:
            citations.append({"source_id": source_id})
            seen.add(source_id)
    return citations


def audited_summary(summary: dict, thread: dict) -> dict:
    """Hide uncited or cross-thread claims without changing the cached original."""
    valid_ids = {
        str(source["id"]).lower()
        for message in thread.get("messages", [])
        for source in (message, *message.get("attachments", []))
    }
    fields = (
        "key_points", "decisions", "dates_and_deadlines", "action_items",
        "important_attachments",
    )
    payload = dict(summary)
    omitted = 0
    for section in fields:
        kept = []
        for bullet in summary.get(section, []):
            references = [item.lower() for item in _referenced_sources(bullet)]
            if references and all(item in valid_ids for item in references):
                kept.append(bullet)
            else:
                omitted += 1
        payload[section] = kept
    payload["citations"] = summary_citations(payload, thread)
    payload["omitted_count"] = omitted
    return payload


def render_assistant_text(text: str, citations: list[dict]) -> str:
    """Safe formatting with numbered links only to validated supplied sources."""
    identifier = _SOURCE_ID
    sources = {str(item.get("source_id", "")).lower(): index
               for index, item in enumerate(citations, 1)
               if re.fullmatch(identifier, str(item.get("source_id", "")))}
    replacements = {}
    nonce = uuid4().hex

    def linked_source(source: str) -> str:
        source = source.lower()
        if source not in sources:
            return "[Source unavailable]"
        marker = f"CITATION{nonce}REF{len(replacements)}END"
        replacements[marker] = (source, sources[source])
        return marker

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _BRACKET_SOURCE.sub(lambda match: linked_source(match.group(1)), text)
    text = _NATIVE_SOURCE.sub(
        lambda match: " ".join(linked_source(source) for source in match.group(1).split("")),
        text,
    )
    text = re.sub(r"cite.*?", "[Source unavailable]", text)
    soup = BeautifulSoup(render_message(text, "", []).html, "html.parser")
    # Assistant text belongs to the application surface, unlike sender HTML.
    for anchor in soup.find_all("a"):
        anchor["style"] = "color:#934735;text-decoration:underline"
    # Clean only visible text: a model can put a stray control character in a
    # descriptive link label. Do not rewrite hrefs or the saved answer.
    for node in list(soup.find_all(string=True)):
        visible = re.sub(r"[ \t]*[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f][ \t]*", " ", str(node))
        pieces = re.split(r"(\*\*[^*\n]+\*\*)", visible)
        if len(pieces) == 1:
            if visible != str(node):
                node.replace_with(visible)
            continue
        for piece in pieces:
            if piece.startswith("**") and piece.endswith("**"):
                strong = soup.new_tag("strong")
                strong.string = piece[2:-2]
                node.insert_before(strong)
            elif piece:
                node.insert_before(NavigableString(piece))
        node.extract()
    markup = str(soup)
    for marker, (source, number) in replacements.items():
        markup = markup.replace(
            marker,
            f'<a href="emailreader://source/{source}" '
            f'style="color:#934735;text-decoration:underline">[{number}]</a>',
        )
    return markup


def safe_link(value: str) -> str:
    value = value.strip()
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            _ = parsed.port  # Reject invalid ports rather than handing Qt a broken link.
            return value
        if parsed.scheme.lower() == "mailto" and "@" in parsed.path:
            return value
    except ValueError:
        pass
    return ""


def link_label(value: str) -> str:
    parsed = urlsplit(value)
    return unquote(parsed.path) if parsed.scheme.lower() == "mailto" else (parsed.hostname or value)


_EMAIL_ADDRESS = (
    r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)*\."
    r"(?:xn--[a-z0-9-]{2,}|[^\W\d_]{2,})"
)

_TOKEN = re.compile(
    r"\[(?P<md_label>[^\]\n]{1,180})\]\s*\((?P<md_target>(?:https?://|mailto:)(?:[^()\s<>]|\([^()\s<>]*\))*)\)"
    r"|\[(?!cid:)(?P<outlook_label>[^\]\n]{1,180})\]\s*<(?P<outlook_target>(?:https?://|mailto:)[^<>\s]+)>"
    r"|\[cid:[^\]]+\]|<cid:[^<>]+>|cid:[^\s<>\]]+"
    r"|(?P<paired_email>" + _EMAIL_ADDRESS + r")\s*<mailto:(?P=paired_email)>"
    r"|(?:https?://|www\.)[^\s<>]+\s*<https?://[^<>]+>"
    r"|<(?P<angle_target>(?:https?://|mailto:)[^<>\s]+)>"
    r"|(?:https?://|mailto:|www\.)[^\s<>]+"
    r"|" + _EMAIL_ADDRESS,
    re.I,
)


def preview_snippet(value: str) -> str:
    """Keep inbox previews readable without changing stored mail or link targets."""
    value = repair_common_utf8_mojibake(value or "")
    # Some text/plain mail contains a pasted or malformed HTML anchor. Keep
    # its readable label in the preview; the full message retains its link.
    value = re.sub(r"</?a\b[^<>]*>", " ", value, flags=re.I)
    # Legacy previews can be truncated entirely inside a sender's repeated
    # zero-width layout filler. Hide that filler without rewriting original
    # message text or removing a single meaningful joiner from another script.
    filler = r"(?:[\u200b\u200c\u200d\u2060\ufeff]\s*){3,}"
    had_filler = bool(re.search(filler, value))
    value = re.sub(filler, " ", value)
    if had_filler:
        # The old 240-character cut can stop halfway through the next
        # mojibake token, leaving an orphan leading 'â' after cleanup.
        value = re.sub(r"\s*â(?:[\x80-\x9f])?$", "", value)
    value = re.sub(
        r"(?P<address>" + _EMAIL_ADDRESS + r")\s*<mailto:(?P=address)>",
        lambda match: match["address"],
        value,
        flags=re.I,
    )
    # Legacy snippets are cut to 240 characters on import, sometimes halfway
    # through a bracketed destination. Do not display the broken fragment.
    value = re.sub(r"<(?:https?://|mailto:|www\.)[^\s<>]*$", "", value, flags=re.I)
    # The same cut can land just after a Markdown link's opening parenthesis,
    # before any destination is available. Retain the useful label, not the
    # dangling link syntax; the original message and URL remain untouched.
    value = re.sub(
        r"\[(?P<label>[^\]\n]{1,180})\]\s*\((?:(?:https?://|mailto:)[^\s)]*)?$",
        lambda match: re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", match["label"]),
        value,
        flags=re.I,
    )

    def replace(match: re.Match[str]) -> str:
        token = match.group()
        if token.lower().startswith(("[cid:", "<cid:", "cid:")):
            return ""
        if match["md_target"]:
            label = match["md_label"].strip()
            if re.fullmatch(r"(?:https?://|mailto:|www\.)\S+", label, re.I):
                return link_label(match["md_target"])
            return label
        if match["outlook_target"]:
            label = match["outlook_label"].strip()
            if re.fullmatch(r"(?:https?://|mailto:|www\.)\S+", label, re.I):
                return link_label(match["outlook_target"])
            return label
        if match["paired_email"]:
            return match["paired_email"]
        if match["angle_target"]:
            return link_label(match["angle_target"])
        if "<http" in token.lower():
            return link_label(token.split("<", 1)[1].rstrip(">"))
        target = token.rstrip(".,;:!?)")
        suffix = token[len(target):]
        if target.lower().startswith("www."):
            target = "https://" + target
        if safe_link(target):
            return link_label(target) + suffix
        return token

    return re.sub(r"\s+", " ", _TOKEN.sub(replace, value)).strip()


def fit_message_images(markup: str, width: int) -> str:
    """Legacy QML-text sizing without throwing away full-resolution image data."""
    bounded = max(1, min(560, int(width) - 2))
    return re.sub(
        r'(src="image://mail/[^"?]+)(?:\?width=\d+)?"',
        lambda match: f'{match[1]}?width={bounded}"',
        markup,
    )


def _flatten_layout_tables(soup: BeautifulSoup) -> None:
    """Let one-column email layout wrappers use the available reading width."""
    for table in reversed(soup.find_all("table")):
        sections = [child for child in table.children
                    if isinstance(child, Tag) and child.name in {"tbody", "thead", "tfoot"}]
        rows = [child for child in table.children
                if isinstance(child, Tag) and child.name == "tr"]
        for section in sections:
            rows.extend(child for child in section.children
                        if isinstance(child, Tag) and child.name == "tr")
        if not rows:
            continue
        cells_by_row = [
            [child for child in row.children
             if isinstance(child, Tag) and child.name in {"td", "th"}]
            for row in rows
        ]
        if any(len(cells) != 1 or cells[0].has_attr("colspan") or cells[0].has_attr("rowspan")
               for cells in cells_by_row):
            continue
        table.name = "div"
        for section in sections:
            section.name = "div"
        for row, cells in zip(rows, cells_by_row, strict=True):
            row.name = "div"
            cells[0].name = "div"


@dataclass
class RenderedMessage:
    html: str = ""
    embedded_ids: set[str] = field(default_factory=set)
    remote_urls: set[str] = field(default_factory=set)


def render_message(
    body_text: str,
    body_html: str,
    attachments: list[dict],
    *,
    quoted: bool = True,
    remote_images: dict[str, str] | None = None,
) -> RenderedMessage:
    body_text = repair_common_utf8_mojibake(body_text)
    body_html = repair_common_utf8_mojibake(body_html)
    result = RenderedMessage()
    images: dict[str, dict] = {}
    for item in attachments:
        if not is_mail_image(item.get("mime_type", ""), item.get("filename", "")):
            continue
        for key in (
            item.get("content_id"),
            item.get("content_location"),
            item.get("provider_id"),
            item.get("filename"),
        ):
            if key:
                images[str(key).strip("<>").casefold()] = item

    def image(source: str, alt: str = "") -> str:
        original_source = source.strip()
        lookup = unquote(original_source[4:] if original_source.lower().startswith("cid:") else original_source)
        key = lookup.strip("<>").casefold()
        item = images.get(key) or images.get(key.split("@", 1)[0])
        if item:
            result.embedded_ids.add(item["id"])
            if item.get("content_digest"):
                identifier = html.escape(item["id"], quote=True)
                label = html.escape(alt or item.get("filename") or "Embedded image", quote=True)
                return f'<img src="image://mail/{identifier}" alt="{label}" />'
            return f'<span class="image-state missing">{html.escape(alt or "Embedded image unavailable")}</span>'
        if original_source.lower().startswith("cid:"):
            return f'<span class="image-state missing">{html.escape(alt or "Embedded image unavailable")}</span>'
        data_match = re.fullmatch(
            r"data:(image/(?:png|jpeg|gif|webp|bmp));base64,([A-Za-z0-9+/=\s]+)",
            original_source,
            re.I,
        )
        if data_match:
            try:
                payload = b64decode(re.sub(r"\s+", "", data_match.group(2)), validate=True)
            except (ValueError, Base64Error):
                payload = b""
            if 0 < len(payload) <= 10 * 1024 * 1024:
                source_value = html.escape(original_source, quote=True)
                return f'<img src="{source_value}" alt="{html.escape(alt, quote=True)}" />'
            return f'<span class="image-state unsupported">{html.escape(alt or "Unsupported embedded image")}</span>'
        source = safe_link(source)
        if source and source.lower().startswith(("https://", "http://")):
            result.remote_urls.add(source)
            if remote_images and source in remote_images:
                return (
                    f'<img src="image://mail/{html.escape(remote_images[source], quote=True)}" />'
                )
            return (
                '<span class="image-state blocked">'
                + html.escape(alt or "Remote image blocked")
                + "</span>"
            )
        state = "missing" if not original_source else "unsupported"
        label = alt or ("Image missing" if state == "missing" else "Unsupported image")
        return f'<span class="image-state {state}">{html.escape(label)}</span>'

    def plain(value: str, *, autolink: bool = True) -> str:
        output = []
        previous = 0
        for match in _TOKEN.finditer(value):
            output.append(html.escape(value[previous : match.start()]))
            token = match.group()
            if match["md_label"] is not None:
                target = safe_link(match["md_target"])
                output.append(
                    f'<a href="{html.escape(target, quote=True)}" title="{html.escape(target, quote=True)}" style="color:#075FCA;text-decoration:underline">{html.escape(match["md_label"])}</a>'
                    if autolink and target else html.escape(token))
            elif match["outlook_label"] is not None:
                target = safe_link(match["outlook_target"])
                label = match["outlook_label"].strip()
                if re.fullmatch(r"(?:https?://|mailto:|www\.)\S+", label, re.I):
                    label = link_label(target)
                output.append(
                    f'<a href="{html.escape(target, quote=True)}" title="{html.escape(target, quote=True)}" style="color:#075FCA;text-decoration:underline">{html.escape(label)}</a>'
                    if autolink and target else html.escape(token))
            elif match["paired_email"] is not None:
                target = safe_link("mailto:" + match["paired_email"])
                output.append(
                    f'<a href="{html.escape(target, quote=True)}" title="{html.escape(target, quote=True)}" style="color:#075FCA;text-decoration:underline">{html.escape(match["paired_email"])}</a>'
                    if autolink and target else html.escape(token))
            elif match["angle_target"] is not None:
                target = safe_link(match["angle_target"])
                output.append(
                    f'<a href="{html.escape(target, quote=True)}" title="{html.escape(target, quote=True)}" style="color:#075FCA;text-decoration:underline">{html.escape(link_label(target))}</a>'
                    if autolink and target else html.escape(token)
                )
            elif token.lower().startswith(("[cid:", "<cid:", "cid:")):
                output.append(image(token[1:-1] if token[0] in "[<" else token))
            elif autolink:
                target = token.split("<", 1)[-1].rstrip(">") if "<" in token else token
                # Leave sentence punctuation outside the link.
                suffix = ""
                prefix = ""
                before = value[match.start() - 1] if match.start() else ""
                after = value[match.end():match.end() + 1]
                closing_quote = {"'": "'", '"': '"', "“": "”", "‘": "’"}.get(before, "")
                # Apostrophes are valid inside email local parts. Strip one
                # only when it is visibly paired with a surrounding quote.
                if target.startswith("'") and after == "'":
                    prefix, target = "'", target[1:]
                while target and (
                    target[-1] in ".,;!?"
                    or (target[-1] == ")" and target.count(")") > target.count("("))
                    or (target[-1] == "]" and target.count("]") > target.count("["))
                    or (target[-1] == "}" and target.count("}") > target.count("{"))
                    or (closing_quote and target[-1] == closing_quote)
                ):
                    if target[-1] == closing_quote:
                        closing_quote = ""
                    suffix = target[-1] + suffix
                    target = target[:-1]
                if target.lower().startswith("www."):
                    target = "https://" + target
                elif ":" not in target and "@" in target:
                    target = "mailto:" + target
                if safe_link(target):
                    output.append(
                        html.escape(prefix)
                        + f'<a href="{html.escape(target, quote=True)}" title="{html.escape(target, quote=True)}" style="color:#075FCA;text-decoration:underline">{html.escape(link_label(target))}</a>'
                        + html.escape(suffix)
                    )
                else:
                    output.append(html.escape(token))
            else:
                output.append(html.escape(token))
            previous = match.end()
        output.append(html.escape(value[previous:]))
        return "".join(output)

    def remove_intentionally_hidden(root: BeautifulSoup) -> None:
        for element in list(root.find_all(True)):
            if element.attrs is None:
                continue
            style = re.sub(r"\s+", "", str(element.get("style", "")).casefold())
            explicitly_hidden = (
                element.has_attr("hidden")
                or str(element.get("aria-hidden", "")).casefold() == "true"
                or "display:none" in style
                or "visibility:hidden" in style
                or re.search(r"(?:^|;)opacity:0(?:[;]|$)", style)
                or (
                    ("max-height:0" in style or "height:0" in style)
                    and "overflow:hidden" in style
                )
                or (
                    re.search(r"font-size:(?:0|1px)(?:[;]|$)", style)
                    and "overflow:hidden" in style
                )
            )
            if explicitly_hidden:
                element.decompose()

    # Mail sometimes supplies a blank HTML alternative alongside populated
    # plain text. An image-only alternative is not blank: discarding it would
    # make a real inline picture disappear from the conversation.
    html_probe = BeautifulSoup(body_html, "html.parser") if body_html else None
    if html_probe:
        remove_intentionally_hidden(html_probe)
    probe_text = (
        html_probe.get_text(" ", strip=True).translate(_INVISIBLE_TRANSLATION)
        if html_probe else ""
    )
    image_only_html = bool(html_probe and html_probe.find("img") and not probe_text.strip())
    if not body_html or (body_text.strip() and not probe_text.strip() and not image_only_html):
        result.html = plain(body_text).replace("\n", "<br />")
        return result
    # Some Mail-produced HTML parts begin with a lone subtype marker. It is
    # transport metadata, not a word from the sender's visible message.
    body_html = re.sub(r"^\s*html\s*\n+(?=\s*<(?:html|body|div|table)\b)", "", body_html, count=1, flags=re.I)
    soup = BeautifulSoup(body_html, "html.parser")
    remove_intentionally_hidden(soup)
    # Apple Mail may serialize a subtype marker before or inside the first
    # layout wrapper. Remove it only if it is the first visible HTML text node
    # and is absent from the sender's plain-text version of the message.
    if soup.find(True) and not re.match(r"^\s*html\b", body_text, re.I):
        for node in soup.descendants:
            if isinstance(node, _NONCONTENT_NODES) or not isinstance(node, NavigableString):
                continue
            if not node.strip():
                continue
            if node.strip().lower() == "html":
                node.extract()
            break
    # Remove only structurally trivial one-cell wrapper tables. Data tables,
    # multi-column layouts, spans and sender content remain intact.
    _flatten_layout_tables(soup)
    if not quoted:
        for node in soup.select('.gmail_quote, .yahoo_quoted, blockquote[type="cite"]'):
            node.decompose()
    # Merge decoration-only wrappers before link detection so an address or
    # URL split by mail-client markup is still one exact clickable target.
    # Styled wrappers are preserved for the HTML renderer below.
    for wrapper in list(soup.find_all(["span", "font"])):
        if not wrapper.get("style") and not any(
            wrapper.get(name) for name in ("color", "face", "size")
        ):
            wrapper.unwrap()
    soup.smooth()
    passive = {
        "p",
        "div",
        "br",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "ul",
        "ol",
        "li",
        "blockquote",
        "table",
        "tbody",
        "thead",
        "tr",
        "td",
        "th",
        "hr",
        "h1",
        "h2",
        "h3",
        "pre",
        "span",
        "font",
        "center",
        "section",
        "article",
    }
    blocked = {
        "script",
        "style",
        "head",
        "title",
        "meta",
        "link",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "form",
        "input",
        "textarea",
        "video",
        "audio",
        "source",
    }

    safe_style_names = {
        "background", "background-color", "border", "border-bottom", "border-color",
        "border-left", "border-radius", "border-right", "border-style", "border-top",
        "border-width", "color", "font-family", "font-size", "font-style", "font-weight",
        "height", "letter-spacing", "line-height", "margin", "margin-bottom", "margin-left",
        "margin-right", "margin-top", "max-width", "min-width", "padding", "padding-bottom",
        "padding-left", "padding-right", "padding-top", "text-align", "text-decoration",
        "vertical-align", "white-space", "width", "word-break", "word-wrap",
    }

    def safe_style(value: str) -> str:
        declarations = []
        for declaration in value.split(";"):
            name, separator, raw = declaration.partition(":")
            name = name.strip().lower()
            raw = raw.strip()
            lowered = raw.lower()
            if (
                separator
                and name in safe_style_names
                and raw
                and "url(" not in lowered
                and "expression" not in lowered
                and "javascript:" not in lowered
                and "-moz-binding" not in lowered
            ):
                declarations.append(f"{name}:{raw}")
        return ";".join(declarations)

    def safe_attributes(node: Tag) -> str:
        values: list[str] = []
        style = safe_style(str(node.get("style", "")))
        if style:
            values.append(f'style="{html.escape(style, quote=True)}"')
        for name in ("colspan", "rowspan", "align", "valign", "width", "height", "bgcolor"):
            raw = str(node.get(name, "")).strip()
            if raw and re.fullmatch(r"[#(),.%\-+\w ]{1,80}", raw):
                values.append(f'{name}="{html.escape(raw, quote=True)}"')
        return (" " + " ".join(values)) if values else ""

    def visit(node, in_link: bool = False) -> str:
        # BeautifulSoup treats document declarations as string subclasses.
        # They describe the transport/document format, not visible mail text;
        # autolinking them leaks XML headers and creates spurious DTD links.
        if isinstance(node, _NONCONTENT_NODES):
            return ""
        if isinstance(node, NavigableString):
            value = str(node)
            if any(char in value for char in _INVISIBLE_FILLER) and not value.translate(
                _INVISIBLE_TRANSLATION
            ).strip():
                return ""
            return plain(value, autolink=not in_link)
        if not isinstance(node, Tag) or node.name in blocked:
            return ""
        if node.name == "img":
            return image(str(node.get("src", "")), str(node.get("alt", "")))
        if node.name == "a":
            target = safe_link(str(node.get("href", "")))
            # A broken/unsafe sender anchor must not suppress safe links in
            # its visible text (for example, an email inside href="#").
            contents = "".join(visit(child, in_link or bool(target)) for child in node.children)
            if target:
                label = node.get_text().strip()
                # A linked signature image can be a 1x1 tracking-sized file.
                # Keep the picture in place, but give image-only links a
                # readable target rather than a one-pixel click area.
                if not label and (linked_image := node.find("img")) is not None:
                    image_label = str(linked_image.get("alt", "")).strip()[:80]
                    fallback = html.escape(image_label or link_label(target))
                    contents = (contents + " " + fallback) if contents.strip() else fallback
                if (
                    not contents.strip()
                    or label == target
                    or label.rstrip("/") == target.rstrip("/")
                    or re.fullmatch(r"(?:https?://|mailto:|www\.)\S+", label, re.I)
                ):
                    contents = html.escape(link_label(target))
                if node.find("button") is not None:
                    return f'<a href="{html.escape(target, quote=True)}" title="{html.escape(target, quote=True)}" class="sender-button">{contents}</a>'
                return (
                    f'<a href="{html.escape(target, quote=True)}" '
                    f'title="{html.escape(target, quote=True)}" '
                    f'style="color:#075FCA;text-decoration:underline">{contents}</a>'
                )
            return contents
        if node.name == "button":
            contents = "".join(visit(child, in_link) for child in node.children)
            return f'<span class="sender-button">{contents}</span>'
        contents = "".join(visit(child, in_link) for child in node.children)
        if node.name not in passive:
            return contents
        if node.name in {"br", "hr"}:
            return f"<{node.name} />"
        if node.name in {"p", "div", "table", "tbody", "thead", "tr", "td", "th"} and not contents.strip():
            return ""
        markup = f"<{node.name}{safe_attributes(node)}>{contents}</{node.name}>"
        if node.name == "table":
            return f'<div class="table-scroll">{markup}</div>'
        return markup

    result.html = "".join(visit(child) for child in soup.children)
    # Qt's rich-text subset can append a trailing inline object to a paragraph
    # that already ended. Preserve the block boundary for following images.
    result.html = re.sub(r"(</(?:p|div|h[1-3])>)\s*(?=<img\b)", r"\1<br />", result.html)
    rendered_text = BeautifulSoup(result.html, "html.parser").get_text(" ", strip=True)
    plain_words = re.findall(r"[\w@.-]+", body_text.casefold())
    rendered_words = set(re.findall(r"[\w@.-]+", rendered_text.casefold()))
    represented = (
        sum(1 for word in plain_words if word in rendered_words) / len(plain_words)
        if plain_words else 1.0
    )
    needs_plain_fallback = bool(
        body_text.strip()
        and html_probe
        and html_probe.find("img")
        and (image_only_html or represented < 0.5)
    )
    if needs_plain_fallback:
        text_result = render_message(body_text, "", attachments)
        # Keep CID images at their plain-text marker positions and add only
        # HTML images that the plain alternative did not already show.
        remaining = BeautifulSoup(result.html, "html.parser")
        for pictured in remaining.find_all("img", src=True):
            source = str(pictured["src"])
            if source.startswith("image://mail/") and unquote(source.removeprefix("image://mail/")) in text_result.embedded_ids:
                pictured.decompose()
        remaining_html = str(remaining) if remaining.find("img") or remaining.get_text(strip=True) else ""
        if text_result.html:
            result.html = text_result.html + ("<br />" + remaining_html if remaining_html else "")
            result.embedded_ids.update(text_result.embedded_ids)
    # HTML mail often splits a URL or address across decorative span/font
    # wrappers. Those wrappers are intentionally discarded above, so the
    # adjacent visible text can be linked in a second safe pass. Existing
    # anchors and image resources are left intact; only text outside links is
    # reconsidered, using the same URL validator as the first pass.
    linked = BeautifulSoup(result.html, "html.parser")
    changed = False
    for node in list(linked.find_all(string=True)):
        if node.find_parent("a") is not None:
            continue
        markup = plain(str(node))
        if "<a href=" not in markup:
            continue
        replacement = BeautifulSoup(markup, "html.parser")
        for part in list(replacement.contents):
            node.insert_before(part.extract())
        node.extract()
        changed = True
    if changed:
        result.html = str(linked)
    return result


_IMAGE_PROVIDER_SOURCE = re.compile(r'(src=")image://mail/([^"?]+)(?:\?width=\d+)?(")')


def image_data_urls(attachments: list[dict], blobs) -> dict[str, str]:
    """Build transient in-memory data URLs for supported encrypted originals."""
    urls: dict[str, str] = {}
    for item in attachments:
        digest = item.get("content_digest")
        if not digest or not is_mail_image(item.get("mime_type", ""), item.get("filename", "")):
            continue
        try:
            content = blobs.get(digest)
        except Exception:
            continue
        mime = str(item.get("mime_type") or "image/png")
        urls[str(item["id"])] = f"data:{mime};base64,{b64encode(content).decode('ascii')}"
    return urls


_KEY_DETAIL = re.compile(
    r"(?<!\w)(?:"
    r"(?:US\$|CA\$|AU\$|€|£|¥|\$)\s?\d[\d,]*(?:\.\d{1,2})?"
    r"|\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|CAD|AUD|EUR|GBP)\b"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?"
    r"|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?::\d{2})?\s?(?:(?:am|pm)\b|(?:a\.m\.|p\.m\.)(?=\s|$))"
    r"|(?:deadline|due)\s+(?:is\s+|on\s+|by\s+)?"
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?)"
    r")",
    re.I,
)


def highlight_key_details(fragment: str) -> str:
    """Highlight only literal details already present in the sanitized source."""
    soup = BeautifulSoup(fragment, "html.parser")
    for node in list(soup.find_all(string=True)):
        if node.find_parent(["style", "script", "mark"]) is not None:
            continue
        value = str(node)
        matches = list(_KEY_DETAIL.finditer(value))
        if not matches:
            continue
        cursor = 0
        for match in matches:
            if match.start() > cursor:
                node.insert_before(NavigableString(value[cursor:match.start()]))
            mark = soup.new_tag("mark")
            mark["class"] = "key-detail"
            mark["title"] = "Exact detail from this message; no current status inferred"
            mark.string = match.group(0)
            node.insert_before(mark)
            cursor = match.end()
        if cursor < len(value):
            node.insert_before(NavigableString(value[cursor:]))
        node.extract()
    return str(soup)


def email_document(
    fragment: str,
    *,
    images: dict[str, str] | None = None,
    reading_mode: bool = False,
    highlight_details: bool = False,
) -> str:
    """Wrap sanitized message markup in a network-isolated HTML document."""
    images = images or {}

    def embedded(match: re.Match[str]) -> str:
        identifier = unquote(match.group(2))
        data_url = images.get(identifier)
        if not data_url:
            return 'src="" data-image-state="missing"'
        return (
            f'src="{data_url}" data-image-state="available" '
            f'data-image-id="{html.escape(identifier, quote=True)}"'
        )

    fragment = _IMAGE_PROVIDER_SOURCE.sub(embedded, fragment)
    document_fragment = BeautifulSoup(fragment, "html.parser")
    for pictured in document_fragment.find_all("img", attrs={"data-image-id": True}):
        if pictured.find_parent("a") is not None:
            continue
        identifier = str(pictured.get("data-image-id", ""))
        preview = document_fragment.new_tag(
            "a",
            href=f"emailreader://preview/{identifier}",
            attrs={"class": "image-link", "title": "Open full-resolution preview"},
        )
        pictured.wrap(preview)
    fragment = str(document_fragment)
    if reading_mode and highlight_details:
        fragment = highlight_key_details(fragment)
    classes = ["reading-mode" if reading_mode else "original-mode"]
    if highlight_details:
        classes.append("highlight-details")
    class_name = " ".join(classes)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline';">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; width: 100%; overflow-x: hidden; }}
body {{ color: #172033; background: transparent; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 16px; line-height: 1.52; overflow-wrap: anywhere; }}
body.reading-mode {{ font-size: 17px; line-height: 1.62; }}
body.reading-mode * {{ color: inherit !important; background-image: none !important; }}
p {{ margin: .55em 0; }}
h1, h2, h3 {{ color: #101828; line-height: 1.24; margin: .8em 0 .35em; }}
blockquote {{ border-left: 3px solid #98a2b3; color: #475467; margin: .75em 0; padding: .15em 0 .15em .9em; }}
a {{ color: #075fca !important; text-decoration: underline !important; text-decoration-thickness: 1.5px !important; text-underline-offset: 2px; cursor: pointer; }}
a:focus-visible {{ outline: 3px solid #7a2e0b; outline-offset: 3px; border-radius: 2px; }}
.sender-button {{ display: inline-block; background: #075fca !important; color: #fff !important; border: 2px solid #075fca; border-radius: 6px; padding: .52em .85em; font-weight: 650; text-decoration: none !important; }}
.table-scroll {{ display: block; width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
table {{ border-collapse: collapse; max-width: none; }}
td, th {{ vertical-align: top; }}
img {{ display: inline-block; max-width: 100% !important; height: auto !important; object-fit: contain; vertical-align: middle; }}
img[data-image-state='missing'] {{ display: none; }}
.image-state {{ display: inline-block; border: 1px solid #667085; border-radius: 5px; padding: .35em .55em; color: #344054 !important; background: #f2f4f7 !important; font-size: .9em; }}
.image-state.blocked::before {{ content: 'Image blocked · '; font-weight: 650; }}
.image-state.missing::before {{ content: 'Image missing · '; font-weight: 650; }}
.image-state.unsupported::before {{ content: 'Unsupported image · '; font-weight: 650; }}
hr {{ border: 0; border-top: 1px solid #b8c2d1; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
mark.key-detail {{ background: #fff0a6 !important; color: #172033 !important; border-bottom: 2px solid #9a6700; border-radius: 2px; padding: 0 .08em; }}
</style></head><body class="{class_name}"><div id="mail-content">{fragment}</div></body></html>"""


_EXCERPT_HTML_TAG = re.compile(
    r"</?(?:a|b|blockquote|br|div|em|h[1-6]|i|li|ol|p|span|strong|table|td|th|tr|u|ul)\b",
    re.I,
)


def render_source_excerpt(value: str) -> str:
    """Display retrieved snippets without leaking raw markup or loading media.

    Some Apple Mail plain-text alternatives contain literal HTML formatting.
    Recognize common layout tags, then pass the fragment through the same
    passive sanitizer as full messages. Ordinary plain text remains plain, so
    bracketed URLs such as ``name<https://...>`` retain their link behavior.
    """
    value = value or ""
    if _EXCERPT_HTML_TAG.search(value):
        return render_message("", value, []).html
    return render_message(value, "", []).html
