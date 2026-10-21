from __future__ import annotations

import re
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from email_reader.contracts import AttachmentPayload, MessagePayload
from email_reader.image_types import is_mail_image, raster_mime
from email_reader.text import (
    decode_mime_header,
    html_to_text,
    looks_like_mislabeled_html,
    normalize_message_text,
    repair_common_utf8_mojibake,
)


def _addresses(message: Message, header: str) -> list[str]:
    values = message.get_all(header, [])
    return [address for _, address in getaddresses(values) if address]


def message_ids(value: str) -> list[str]:
    """Extract identifiers, never arbitrary text that could become a header."""
    return list(dict.fromkeys(re.findall(r"<[^<>\s]+@[^<>\s]+>", value)))


def reply_headers(raw: bytes) -> dict:
    # Legacy saved originals can supply headers without re-downloading mail or
    # decoding attachment bodies. Read only the RFC header section.
    header = re.split(br"\r?\n\r?\n", raw, maxsplit=1)[0]
    message = BytesHeaderParser(policy=policy.default).parsebytes(header + b"\r\n\r\n")
    return {
        "reply_to": _addresses(message, "Reply-To"),
        "references": message_ids(str(message.get("References", ""))),
    }


def _date(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        result = parsedate_to_datetime(value)
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(UTC)


def parse_rfc822(
    raw: bytes,
    *,
    provider_id: str,
    provider_thread_id: str,
    labels: list[str] | None = None,
    web_link: str | None = None,
) -> MessagePayload:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender_name, sender_email = (
        getaddresses([message.get("From", "")])[0] if message.get("From") else ("", "")
    )
    body_text = ""
    body_html = ""
    attachments: list[AttachmentPayload] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = decode_mime_header(part.get_filename())
        payload = part.get_payload(decode=True) or b""
        content_id = str(part.get("Content-ID", "")).strip("<>")
        content_location = str(part.get("Content-Location", "")).strip()
        if content_type in {"application/octet-stream", "binary/octet-stream"}:
            content_type = raster_mime(payload) or content_type
        if (
            disposition == "attachment"
            or filename
            or ((content_id or content_location) and is_mail_image(content_type, filename))
        ):
            attachments.append(
                AttachmentPayload(
                    provider_id=(
                        part.get("Content-ID")
                        or content_location
                        or filename
                        or str(len(attachments))
                    ),
                    filename=filename or f"attachment-{len(attachments) + 1}",
                    mime_type=content_type,
                    size=len(payload),
                    content=payload,
                    is_inline=disposition == "inline"
                    or bool((content_id or content_location) and is_mail_image(content_type, filename)),
                    content_id=content_id,
                    content_location=content_location,
                )
            )
            continue
        charset = part.get_content_charset() or "utf-8"
        decoded = repair_common_utf8_mojibake(payload.decode(charset, errors="replace"))
        if content_type == "text/plain" and not body_text:
            body_text = decoded
        elif content_type == "text/html" and not body_html:
            body_html = decoded
    if not body_text and body_html:
        body_text = html_to_text(body_html)
    elif body_text and looks_like_mislabeled_html(body_text):
        # Some senders label HTML, including wrapper-free table fragments, as
        # text/plain. Preserve the original markup for passive rendering.
        body_html = body_html or body_text
        body_text = html_to_text(body_text)
    body_text = normalize_message_text(body_text)
    labels = labels or []
    sent_at = _date(message.get("Date"))
    return MessagePayload(
        provider_id=provider_id,
        provider_thread_id=provider_thread_id,
        internet_message_id=message.get("Message-ID"),
        in_reply_to=message.get("In-Reply-To"),
        references=message_ids(str(message.get("References", ""))),
        reply_to=_addresses(message, "Reply-To"),
        subject=decode_mime_header(message.get("Subject")) or "(no subject)",
        sender_name=decode_mime_header(sender_name),
        sender_email=sender_email,
        to=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        bcc=_addresses(message, "Bcc"),
        sent_at=sent_at,
        received_at=sent_at,
        body_text=body_text,
        body_html=body_html,
        snippet=body_text[:240].replace("\n", " "),
        labels=labels,
        is_sent="SENT" in labels,
        is_draft="DRAFT" in labels,
        is_unread="UNREAD" in labels,
        raw_source=raw,
        source_complete=str(message.get("X-Email-Reader-Deferred-Source", "")).lower() != "true",
        web_link=web_link,
        attachments=attachments,
    )
