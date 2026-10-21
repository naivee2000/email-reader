"""Manual draft preparation. This module has no provider or sending operations."""

import re
from email.utils import getaddresses
from urllib.parse import unquote

from bs4 import BeautifulSoup

from email_reader.contracts import DraftRequest
from email_reader.image_types import is_mail_image
from email_reader.providers.mime import message_ids
from email_reader.rendering import link_label, safe_link
from email_reader.text import html_to_text, looks_like_mislabeled_html

_CID_REFERENCE = re.compile(r"\[cid:([^\]\s]+)\]|<cid:([^<>\s]+)>|(?<![\w])cid:([^\s<>\]]+)", re.I)


def _forward_body(message: dict) -> str:
    """Keep readable text and identify any inline images copied as files."""
    original = message.get("body_text") or html_to_text(message.get("body_html") or "")
    if looks_like_mislabeled_html(original):
        original = html_to_text(original)
    image_names: dict[str, str] = {}
    for item in message.get("attachments", []):
        if not is_mail_image(item.get("mime_type", ""), item.get("filename", "")):
            continue
        for key in (item.get("content_id"), item.get("provider_id"), item.get("filename")):
            if key:
                image_names[str(key).strip("<>").casefold()] = item.get("filename") or "image"

    def replace(match: re.Match[str]) -> str:
        key = unquote(next(part for part in match.groups() if part)).strip("<>").casefold()
        name = image_names.get(key) or image_names.get(key.split("@", 1)[0])
        return f"[Inline image attached: {name}]" if name else "[Embedded image unavailable]"

    original = _CID_REFERENCE.sub(replace, original)
    links = []
    seen = set()
    if message.get("body_html"):
        for anchor in BeautifulSoup(message["body_html"], "html.parser").find_all("a", href=True):
            target = safe_link(str(anchor["href"]))
            if not target or target in seen or target in original:
                continue
            seen.add(target)
            label = " ".join(anchor.get_text(" ", strip=True).split())[:160] or link_label(target)
            links.append(f"- {label}: {target}")
    if links:
        original += "\n\nLinks from the original message:\n" + "\n".join(links)
    return original


def addresses(value: str) -> list[str]:
    output = []
    for _, address in getaddresses([value.replace(";", ",")]):
        if not address:
            continue
        if "@" not in address or any(char.isspace() for char in address):
            raise ValueError("Please check the recipient email addresses")
        if address.casefold() not in {item.casefold() for item in output}:
            output.append(address)
    return output


def reply_unavailable(sender_email: str, new_content: str = "") -> bool:
    """Recognize automated notices that explicitly cannot receive a reply."""
    local_part = sender_email.partition("@")[0]
    return bool(
        re.search(r"(?:no[-_.]?reply|do[-_.]?not[-_.]?reply)", local_part, re.I)
        or re.search(r"\breplies?\s+(?:are\s+)?not\s+(?:monitored|answered)\b", new_content, re.I)
    )


def reply_requires_instruction(new_content: str) -> bool:
    """Recognize informational notices that need the user's reply intent.

    This is narrower than an assertion that the sender cannot receive mail: a
    user can still request a specific follow-up or use the manual Reply editor.
    """
    text = " ".join(new_content.split()).casefold()
    paragraphs = re.split(r"\n\s*\n", new_content.strip(), maxsplit=1)
    first_paragraph = paragraphs[0].strip().casefold().rstrip("!,. ")
    signature_only_ack = (
        first_paragraph in {"thank you", "thanks", "thank you very much", "thanks so much"}
        and (
            len(paragraphs) == 1
            or bool(re.search(r"\bpronouns\s*:|\b(?:www\.|https?://)", paragraphs[1], re.I))
        )
    )
    return bool(
        signature_only_ack
        or
        ("you received this email because" in text and
         ("notification settings" in text or "turn off email notifications" in text))
        or "this is a reminder that you are registered for" in text
    )


def initial_draft(
    mode: str, thread: dict, accounts: list[dict], selected_account: str = ""
) -> dict:
    if mode not in {"new", "reply", "reply_all", "forward"}:
        raise ValueError("Unknown draft type")
    account_id = selected_account or (accounts[0]["id"] if accounts else "")
    draft = {
        "mode": mode,
        "account_id": account_id,
        "source_account_id": "",
        "thread_id": "",
        "provider_thread_id": "",
        "reply_to_provider_message_id": "",
        "in_reply_to": "",
        "references": [],
        "to": "",
        "cc": "",
        "bcc": "",
        "subject": "",
        "body": "",
    }
    if mode == "new":
        return draft
    messages = [item for item in thread.get("messages", []) if not item.get("is_draft")]
    if not messages:
        raise ValueError("Open a received or sent email first")
    latest = max(messages, key=lambda item: item["sent_at"])
    draft.update(
        account_id=thread["account_id"],
        source_account_id=thread["account_id"],
        thread_id=thread["id"],
    )
    subject = thread["subject"]
    prefix = "Fwd: " if mode == "forward" else "Re: "
    draft["subject"] = subject if subject.lower().startswith(prefix.lower()) else prefix + subject
    if mode == "forward":
        draft["body"] = (
            "\n\n---------- Forwarded message ----------\n"
            f"From: {latest['sender_name']} <{latest['sender_email']}>\n"
            f"Date: {latest['sent_at']}\nSubject: {latest['subject']}\n"
            f"To: {', '.join(latest['recipients'].get('to', []))}\n\n{_forward_body(latest)}"
        )
        draft["attachment_ids"] = [
            item["id"] for item in latest.get("attachments", [])
        ]
        return draft
    own = {account["email"].casefold() for account in accounts}
    original_to = latest["recipients"].get("to", [])
    sender = latest["sender_email"]
    to = list(latest["recipients"].get("reply_to") or [sender]) if sender.casefold() not in own else list(original_to)
    if mode == "reply_all":
        to += original_to
    draft["to"] = ", ".join(item for item in addresses(", ".join(to)) if item.casefold() not in own)
    if mode == "reply_all":
        used = own | {item.casefold() for item in addresses(draft["to"])}
        draft["cc"] = ", ".join(
            item for item in addresses(", ".join(latest["recipients"].get("cc", [])))
            if item.casefold() not in used
        )
    draft["provider_thread_id"] = thread["provider_thread_id"]
    draft["reply_to_provider_message_id"] = latest["provider_id"]
    parent = message_ids(latest.get("internet_message_id") or "")
    draft["in_reply_to"] = parent[0] if parent else ""
    draft["references"] = message_ids(" ".join([
        *(latest["recipients"].get("references") or message_ids(latest.get("in_reply_to") or "")),
        *parent,
    ]))
    return draft


def draft_request(form: dict, original: dict) -> DraftRequest:
    same_account = form.get("account_id") == original.get("source_account_id")
    return DraftRequest(
        subject=str(form.get("subject", "")),
        body_text=str(form.get("body", "")),
        to=addresses(str(form.get("to", ""))),
        cc=addresses(str(form.get("cc", ""))),
        bcc=addresses(str(form.get("bcc", ""))),
        provider_thread_id=original.get("provider_thread_id") or None if same_account else None,
        reply_to_provider_message_id=original.get("reply_to_provider_message_id") or None
        if same_account
        else None,
        in_reply_to=original.get("in_reply_to") or None,
        references=original.get("references", []),
    )
