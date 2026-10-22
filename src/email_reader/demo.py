from __future__ import annotations

from datetime import UTC, datetime, timedelta

from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind
from email_reader.repository import MailRepository

DEMO_QUESTIONS = (
    "Find my auto insurance documents and coverage end date",
    "Where and when is the university welcome event?",
    "Compare the old and revised vendor renewal proposals",
)


def _demo_pdf(lines: list[str]) -> bytes:
    """Create a real, text-extractable PDF solely for fictional demo mail."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((54, 72), "\n".join(lines), fontsize=12)
    content = document.tobytes()
    document.close()
    return content


def populate_demo(repository: MailRepository) -> None:
    if repository.db.scalar("SELECT 1 FROM messages WHERE provider_id='insurance-renewal'", default=0):
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
    insurance_old = _demo_pdf([
        "FICTIONAL AUTO POLICY - EXPIRED VERSION",
        "Alex Rivera, vehicle 2022 Cedar Sedan",
        "Effective January 1, 2025. Expiration December 31, 2025.",
        "Policy number DEMO-OLD-2511. This is not current coverage.",
    ])
    insurance_new = _demo_pdf([
        "FICTIONAL AUTO POLICY - RENEWAL",
        "Alex Rivera, vehicle 2022 Cedar Sedan",
        "Effective January 1, 2026. Expiration December 31, 2026.",
        "Policy number DEMO-NEW-2611. Proof of insurance.",
    ])
    repository.ingest_message("demo-gmail", MessagePayload(
        provider_id="insurance-old", provider_thread_id="insurance-history",
        subject="Previous auto policy", sender_name="Harper Brooks", sender_email="harper@copper.example",
        to=["alex.personal@example.com"], sent_at=now-timedelta(days=310),
        body_text="Attached is your previous auto policy. A renewal will follow separately.",
        attachments=[AttachmentPayload(provider_id="old-policy", filename="auto-policy-2025.pdf",
                                       mime_type="application/pdf", content=insurance_old, size=len(insurance_old))],
    ))
    repository.ingest_message("demo-gmail", MessagePayload(
        provider_id="insurance-renewal", provider_thread_id="insurance-renewal",
        subject="Your renewed auto policy and proof card", sender_name="Harper Brooks",
        sender_email="harper@copper.example", to=["alex.personal@example.com"],
        sent_at=now-timedelta(days=25),
        body_text="Here are your renewed policy and proof of insurance card. Please use the newer documents.",
        attachments=[
            AttachmentPayload(provider_id="new-policy", filename="auto-policy-2026.pdf",
                              mime_type="application/pdf", content=insurance_new, size=len(insurance_new)),
            AttachmentPayload(provider_id="proof-card", filename="proof-of-insurance.txt",
                              mime_type="text/plain", content=b"Fictional proof card. Policy DEMO-NEW-2611. Expiration December 31, 2026."),
        ],
    ))
    repository.ingest_message("demo-work", MessagePayload(
        provider_id="welcome-invite", provider_thread_id="welcome-event",
        subject="University welcome event details", sender_name="Sam Patel",
        sender_email="sam@university.example", to=["alex@northstar.example"],
        sent_at=now-timedelta(days=7),
        body_text="The university welcome event is scheduled for October 28 at 5:30 PM in the North Hall atrium. "
                  "This invitation does not confirm attendance.",
    ))
    repository.ingest_message("demo-work", MessagePayload(
        provider_id="welcome-update", provider_thread_id="welcome-event",
        subject="Re: University welcome event details", sender_name="Sam Patel",
        sender_email="sam@university.example", to=["alex@northstar.example"],
        sent_at=now-timedelta(days=5),
        body_text="Correction: the university welcome event is now scheduled for October 29 at 6 PM "
                  "in the North Hall atrium. Please use the revised date.",
    ))
    proposal_old = _demo_pdf([
        "FICTIONAL VENDOR PROPOSAL - VERSION 1",
        "One-year plan: $48,000. Two-year plan: 7 percent discount.",
        "This version was superseded by a revised proposal.",
    ])
    proposal_new = _demo_pdf([
        "FICTIONAL VENDOR PROPOSAL - VERSION 2",
        "One-year plan: $45,000. Two-year plan: 10 percent discount.",
        "This is a proposal, not a signed agreement.",
    ])
    repository.ingest_message("demo-other", MessagePayload(
        provider_id="vendor-old", provider_thread_id="vendor-old",
        subject="Old vendor renewal proposal", sender_name="Casey Wu",
        sender_email="casey@vendor.example", to=["alex.projects@example.com"],
        sent_at=now-timedelta(days=14), body_text="Please see our initial proposal.",
        attachments=[AttachmentPayload(provider_id="vendor-v1", filename="vendor-proposal-v1.pdf",
                                       mime_type="application/pdf", content=proposal_old, size=len(proposal_old))],
    ))
    repository.ingest_message("demo-other", MessagePayload(
        provider_id="vendor-revised", provider_thread_id="vendor-revised",
        subject="Revised vendor renewal proposal", sender_name="Casey Wu",
        sender_email="casey@vendor.example", to=["alex.projects@example.com"],
        sent_at=now-timedelta(days=2), body_text="The revised proposal replaces version 1. Nothing has been signed.",
        attachments=[AttachmentPayload(provider_id="vendor-v2", filename="vendor-proposal-v2.pdf",
                                       mime_type="application/pdf", content=proposal_new, size=len(proposal_new))],
    ))
    repository.ingest_message("demo-other", MessagePayload(
        provider_id="missing-file", provider_thread_id="missing-file",
        subject="Missing contract appendix", sender_name="Casey Wu", sender_email="casey@vendor.example",
        to=["alex.projects@example.com"], sent_at=now-timedelta(days=1),
        body_text="The appendix was mentioned but the original file is not available in this saved copy.",
        attachments=[AttachmentPayload(provider_id="missing-appendix", filename="contract-appendix.pdf",
                                       mime_type="application/pdf", content=None)],
    ))
