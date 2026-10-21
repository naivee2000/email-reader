from __future__ import annotations

from email.message import EmailMessage

import pytest

from email_reader.categories import classify_inbox_headers, infer_mail_category


def test_classifies_human_mail_as_primary_and_transactional_mail_as_updates() -> None:
    assert infer_mail_category("maya@example.com", "Project review") == "primary"
    assert infer_mail_category("no-reply@bank.example", "Your statement is ready") == "updates"
    assert infer_mail_category("service@example.com", "Payment confirmation") == "updates"


def test_filters_promotions_but_keeps_explicit_gmail_updates() -> None:
    promotion = EmailMessage()
    promotion["X-Gmail-Labels"] = "Inbox,Category Promotions"
    assert classify_inbox_headers(promotion) is None

    update = EmailMessage()
    update["X-Gmail-Labels"] = "Inbox,Category Updates"
    update["List-Unsubscribe"] = "<https://example.com/unsubscribe>"
    assert classify_inbox_headers(update) == "updates"


@pytest.mark.parametrize("header,value", [
    ("List-ID", "PSD students <psd.example.edu>"),
    ("List-Unsubscribe", "<mailto:list@example.edu>"),
    ("Precedence", "list"), ("Precedence", "bulk"),
])
def test_school_and_community_list_mail_is_kept_in_updates(header, value):
    message = EmailMessage()
    message["From"] = "Events <events@example.edu>"
    message["Subject"] = "PSD happy hour this Friday"
    message[header] = value
    assert classify_inbox_headers(message) == "updates"


def test_explicit_junk_is_still_excluded():
    message = EmailMessage()
    message["Precedence"] = "junk"
    message["List-ID"] = "anything"
    assert classify_inbox_headers(message) is None


@pytest.mark.parametrize("category", ["Social", "Forums"])
def test_only_promotions_are_excluded_from_gmail_categories(category):
    message = EmailMessage()
    message["X-Gmail-Labels"] = f"Inbox,Category {category}"
    assert classify_inbox_headers(message) == category.lower()


def test_unknown_category_is_retained_even_for_promotional_sounding_subject():
    message = EmailMessage()
    message["Subject"] = "Sale! Last chance!"
    assert classify_inbox_headers(message) == "primary"
