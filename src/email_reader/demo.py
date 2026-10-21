from __future__ import annotations

from datetime import UTC, datetime, timedelta

from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind
from email_reader.repository import MailRepository


def populate_demo(repository: MailRepository) -> None:
    if repository.list_accounts():
        return
    accounts = [
        Account(
            id="demo-gmail",
            provider=ProviderKind.GMAIL,
            email="alex.personal@example.com",
            display_name="Alex Personal",
            color="#EA4335",
        ),
        Account(
            id="demo-work",
            provider=ProviderKind.OUTLOOK,
            email="alex@northstar.example",
            display_name="Alex at Northstar",
            color="#0078D4",
        ),
        Account(
            id="demo-other",
            provider=ProviderKind.GMAIL,
            email="alex.projects@example.com",
            display_name="Alex Projects",
            color="#34A853",
        ),
    ]
    for account in accounts:
        repository.upsert_account(account)
    now = datetime.now(UTC)
    repository.ingest_message(
        "demo-work",
        MessagePayload(
            provider_id="proposal-1",
            provider_thread_id="proposal-thread",
            subject="Renewal proposal and September timeline",
            sender_name="Maya Chen",
            sender_email="maya@vendor.example",
            to=["alex@northstar.example"],
            sent_at=now - timedelta(days=2),
            received_at=now - timedelta(days=2),
            body_text=(
                "Hi Alex,\n\nAttached is the renewal proposal. The annual fee is $48,000. "
                "Could you confirm by September 12 whether you want Option 1 (one year) or "
                "Option 2 (two years with a 7% discount)?\n\nBest,\nMaya"
            ),
            snippet="Attached is the renewal proposal. Could you confirm by September 12…",
            is_unread=True,
            attachments=[
                AttachmentPayload(
                    provider_id="proposal-file",
                    filename="renewal-options.txt",
                    mime_type="text/plain",
                    content=b"Option 1: one year, $48,000.\nOption 2: two years, 7% discount.\nReply deadline: September 12.",
                    size=102,
                )
            ],
        ),
    )
    repository.ingest_message(
        "demo-gmail",
        MessagePayload(
            provider_id="travel-1",
            provider_thread_id="travel-thread",
            subject="Lake house details",
            sender_name="Jordan Lee",
            sender_email="jordan@example.net",
            to=["alex.personal@example.com"],
            sent_at=now - timedelta(hours=8),
            received_at=now - timedelta(hours=8),
            body_text="The address is 18 Cedar Lane. Check-in is Friday after 3 PM. The door code is 2841.",
            snippet="The address is 18 Cedar Lane. Check-in is Friday after 3 PM…",
        ),
    )
    repository.ingest_message(
        "demo-other",
        MessagePayload(
            provider_id="sent-1",
            provider_thread_id="sent-thread",
            subject="Re: Design review",
            sender_name="Alex",
            sender_email="alex.projects@example.com",
            to=["team@example.org"],
            sent_at=now - timedelta(days=5),
            received_at=now - timedelta(days=5),
            body_text="Hi team,\n\nThanks for putting this together. Tuesday at 2 works well for me. I’ll review the updated mockups beforehand.\n\nBest,\nAlex",
            snippet="Thanks for putting this together. Tuesday at 2 works well for me.",
            labels=["SENT"],
            is_sent=True,
        ),
    )
