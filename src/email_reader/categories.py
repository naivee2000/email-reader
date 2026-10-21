from __future__ import annotations

import re
from email.message import Message
from email.utils import parseaddr

IMPORT_POLICY_VERSION = "promotions-only-v2"
BULK_GMAIL_CATEGORIES = {"category promotions"}
PRIMARY_GMAIL_CATEGORIES = {"category primary", "category personal"}
UPDATES_GMAIL_CATEGORY = "category updates"

_AUTOMATED_SENDER = re.compile(
    r"(?:^|[._-])(no-?reply|do-?not-?reply|notification|notifications|alert|alerts|"
    r"billing|receipt|receipts|security|mailer-daemon)(?:$|[._+-])",
    re.IGNORECASE,
)
_UPDATE_SUBJECT = re.compile(
    r"\b(statement|invoice|receipt|order|shipment|delivery|security alert|"
    r"verification code|password reset|payment|appointment|reservation|"
    r"status update|confirmation|confirmed|renewal notice|action required)\b",
    re.IGNORECASE,
)


def infer_mail_category(sender_email: str, subject: str, auto_submitted: str = "") -> str:
    local_part = sender_email.split("@", 1)[0]
    if auto_submitted and auto_submitted.casefold() != "no":
        return "updates"
    if _AUTOMATED_SENDER.search(local_part) or _UPDATE_SUBJECT.search(subject):
        return "updates"
    return "primary"


def inbox_exclusion_reason(headers: Message) -> str | None:
    """Only explicit evidence can suppress received mail; unknown is retained."""
    gmail_labels = str(headers.get("X-Gmail-Labels", "")).casefold().replace("_", " ")
    if str(headers.get("Precedence", "")).strip().casefold() == "junk":
        return "junk"
    if any(category in gmail_labels for category in BULK_GMAIL_CATEGORIES):
        return "promotions"
    return None


def classify_inbox_headers(headers: Message) -> str | None:
    if inbox_exclusion_reason(headers):
        return None
    gmail_labels = str(headers.get("X-Gmail-Labels", "")).casefold().replace("_", " ")
    if UPDATES_GMAIL_CATEGORY in gmail_labels:
        return "updates"
    if "category social" in gmail_labels:
        return "social"
    if "category forums" in gmail_labels:
        return "forums"
    if any(category in gmail_labels for category in PRIMARY_GMAIL_CATEGORIES):
        return "primary"
    precedence = str(headers.get("Precedence", "")).strip().casefold()
    # A list header describes delivery, not importance. Campus announcements,
    # assessment invitations and community events also use these headers.
    if precedence in {"bulk", "list"} or headers.get("List-Unsubscribe") or headers.get("List-ID"):
        return "updates"
    sender = parseaddr(str(headers.get("From", "")))[1]
    return infer_mail_category(
        sender,
        str(headers.get("Subject", "")),
        str(headers.get("Auto-Submitted", "")),
    )
