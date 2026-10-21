from __future__ import annotations

import html
import re
from email.header import decode_header, make_header

from bs4 import BeautifulSoup

_BROKEN_UTF8_PUNCTUATION = re.compile(r"â[\x80-\x9f][\x80-\xbf]")
_OUTLOOK_IOS_SIGNATURE = re.compile(
    r"^(?:获取|Get)\s+Outlook\s+for\s+iOS(?:<[^>]+>)?$", re.IGNORECASE
)
_INVISIBLE_FILLER = "\u200b\u200c\u200d\u2060\ufeff"
_INVISIBLE_TRANSLATION = str.maketrans("", "", _INVISIBLE_FILLER)


def repair_common_utf8_mojibake(value: str) -> str:
    """Repair UTF-8 punctuation misdecoded as Latin-1 by some Mail sources."""
    return _BROKEN_UTF8_PUNCTUATION.sub(
        lambda match: match.group().encode("latin-1").decode("utf-8"), value
    )

QUOTED_PATTERNS = (
    re.compile(r"^On .+ wrote:$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^From:\s+.+$", re.IGNORECASE),
)


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def html_to_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(repair_common_utf8_mojibake(value), "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta"]):
        tag.decompose()
    for node in soup.find_all(string=True):
        if any(char in node for char in _INVISIBLE_FILLER) and not node.translate(
            _INVISIBLE_TRANSLATION
        ).strip():
            node.extract()
    return normalize_whitespace(soup.get_text("\n"))


def looks_like_mislabeled_html(value: str) -> bool:
    """Recognize full HTML or a table-shaped HTML body sent as text/plain.

    A stray tag in ordinary mail (including code examples) is not enough to
    reinterpret the message. Some Outlook senders omit the html/body wrapper.
    """
    start = value.lstrip()
    if re.match(r"(?:<!doctype\s+html\b|<html\b)", start, re.I):
        return True
    return bool(
        re.match(r"<table\b", start, re.I)
        and re.search(r"<tr\b", start, re.I)
        and re.search(r"<td\b|<th\b", start, re.I)
        and re.search(r"</table\s*>", start, re.I)
    ) or bool(
        # Some bulk templates put one prose line before a complete HTML layout.
        # Require repeated paired structure so a code example stays literal.
        len(re.findall(r"<(?:div|table|tr|td|p)\b", start, re.I)) >= 8
        and len(re.findall(r"</(?:div|table|tr|td|p)\s*>", start, re.I)) >= 8
        and re.search(r"<(?:div|table)\b", start, re.I)
    )


def normalize_whitespace(value: str) -> str:
    value = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    output: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if not (blank and previous_blank):
            output.append(line)
        previous_blank = blank
    return "\n".join(output).strip()


def normalize_message_text(value: str) -> str:
    """Remove layout indentation that makes plain-text email unreadable in narrow panes."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"^(?: {4,}|\t+)", "", line).rstrip(" \t") for line in value.splitlines()]
    return "\n".join(lines).strip()


def strip_quoted_reply(value: str) -> str:
    return message_new_content(value)


def message_new_content(value: str, html_source: str = "") -> str:
    """Hide recognizable quote blocks, preserving inline/bottom replies and the original elsewhere."""
    if html_source:
        soup = BeautifulSoup(html_source, "html.parser")
        quotes = soup.select('.gmail_quote, .yahoo_quoted, blockquote[type="cite"]')
        if quotes:
            for quote in quotes:
                if quote.parent is not None:
                    quote.decompose()
            return message_new_content(html_to_text(str(soup)))
    lines = normalize_message_text(value).splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        # Explicit > quotes can be interleaved with new answers; don't truncate after them.
        if line.startswith(">"):
            index += 1
            continue
        block = " ".join(part.strip() for part in lines[index : index + 12])
        match = re.match(r"On\s+.+?wrote:\s*", block, re.IGNORECASE)
        if match:
            consumed = 0
            while index + consumed < len(lines) and consumed < 12:
                if lines[index + consumed].strip().endswith("wrote:"):
                    break
                consumed += 1
            if consumed < 12:
                following = index + consumed + 1
                next_line = next((part.strip() for part in lines[following:] if part.strip()), "")
                if next_line.startswith(">"):
                    index = following
                    continue
                break
        if re.match(r"^-{2,}\s*(Original Message|Forwarded message)\s*-{2,}", line, re.I):
            break
        if _OUTLOOK_IOS_SIGNATURE.fullmatch(line):
            break
        if re.fullmatch(r"_{10,}", line):
            following = "\n".join(lines[index + 1 : index + 12])
            if re.search(r"^(?:发件人|From):", following, re.M | re.I):
                break
        if re.match(r"^发件人\s*:", line):
            header = "\n".join(lines[index : index + 16])
            if re.search(r"^发送时间\s*:", header, re.M) and re.search(
                r"^(?:收件人|主题)\s*:", header, re.M
            ):
                break
        if re.match(r"^From:", line, re.I):
            header = "\n".join(lines[index : index + 16])
            if re.search(r"^(Sent|Date):", header, re.M | re.I) and re.search(
                r"^(To|Subject):", header, re.M | re.I
            ):
                break
        kept.append(lines[index])
        index += 1
    return "\n".join(kept).strip()


def strip_signature(value: str) -> str:
    lines = value.splitlines()
    for index, line in enumerate(lines):
        if line.strip() in {"--", "—", "Sent from my iPhone", "Sent from Outlook for iOS"} or (
            _OUTLOOK_IOS_SIGNATURE.fullmatch(line.strip())
        ):
            return "\n".join(lines[:index]).strip()
    return value.strip()


def clean_sent_body(value: str) -> str:
    return strip_signature(strip_quoted_reply(value))


def chunk_text(value: str, *, max_chars: int = 2400, overlap: int = 250) -> list[str]:
    value = normalize_whitespace(value)
    if not value:
        return []
    paragraphs = value.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            end = min(len(paragraph), start + max_chars)
            chunks.append(paragraph[start:end])
            if end == len(paragraph):
                break
            start = max(start + 1, end - overlap)
        current = ""
    if current:
        chunks.append(current)
    return chunks


def safe_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w@.-]+", query, flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:16])
