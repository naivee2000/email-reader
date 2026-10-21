from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderKind(StrEnum):
    GMAIL = "gmail"
    OUTLOOK = "outlook"


class SourceState(StrEnum):
    AVAILABLE = "available"
    REMOTE_DELETED = "remote_deleted"
    DISCONNECTED = "disconnected"


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    provider: ProviderKind
    email: str
    display_name: str = ""
    color: str = "#1A73E8"
    enabled: bool = True
    sync_from: datetime | None = None
    last_synced_at: datetime | None = None


class AttachmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str
    filename: str
    mime_type: str = "application/octet-stream"
    size: int = 0
    content: bytes | None = None
    is_inline: bool = False
    content_id: str = ""
    content_location: str = ""


class MessagePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider_id: str
    provider_thread_id: str
    provider_revision: str | None = None
    internet_message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    reply_to: list[str] = Field(default_factory=list)
    subject: str = "(no subject)"
    sender_name: str = ""
    sender_email: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    body_text: str = ""
    body_html: str = ""
    snippet: str = ""
    labels: list[str] = Field(default_factory=list)
    is_sent: bool = False
    is_draft: bool = False
    is_unread: bool = False
    raw_source: bytes | None = None
    source_complete: bool = True
    retry_locator: dict[str, str] = Field(default_factory=dict)
    web_link: str | None = None
    attachments: list[AttachmentPayload] = Field(default_factory=list)


class SyncPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[MessagePayload]
    next_page_token: str | None = None
    next_cursor: str | None = None
    remote_deleted_ids: list[str] = Field(default_factory=list)
    # Number of provider records inspected to produce this page. This can be
    # larger than ``messages`` when junk or bulk mail is filtered locally.
    examined_count: int = 0
    unchanged_read_states: dict[str, bool] = Field(default_factory=dict)
    unchanged_revisions: dict[str, str] = Field(default_factory=dict)
    unchanged_label_states: dict[str, list[str]] = Field(default_factory=dict)
    # Gmail labels include UNREAD; Graph folder roles do not encode read state.
    labels_include_read_state: bool = True
    removed_folder_roles: dict[str, str] = Field(default_factory=dict)
    inbox_scope_ids: list[str] = Field(default_factory=list)
    inbox_scope_reset: bool = False
    inbox_scope_complete: bool = False
    filtered_bulk_ids: list[str] = Field(default_factory=list)
    exclusions: dict[str, str] = Field(default_factory=dict)


class SourceRetryRequest(BaseModel):
    provider_id: str
    locator: dict[str, str] = Field(default_factory=dict)


class SyncStepResult(BaseModel):
    """One durable provider-page turn, suitable for round-robin scheduling."""

    processed: int = 0
    examined: int = 0
    complete: bool = False
    paused: bool = False


class SourceRetryResult(BaseModel):
    provider_id: str
    message: MessagePayload | None = None
    excluded: bool = False
    exclusion_reason: str = ""


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    body_text: str
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    provider_thread_id: str | None = None
    reply_to_provider_message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    attachments: list[AttachmentPayload] = Field(default_factory=list)


class DraftReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_draft_id: str
    provider_message_id: str | None = None
    web_link: str | None = None


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    source_type: str
    account_email: str
    title: str
    sender: str = ""
    sent_at: str = ""
    locator: str = ""
    excerpt: str = ""
    app_link: str
    provider_link: str | None = None


class DraftCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    stance: str
    assumption: str = ""
    subject: str = ""
    body: str
    supporting_source_ids: list[str] = Field(default_factory=list)
    tone_example_ids: list[str] = Field(default_factory=list)


class DraftSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_detected: bool
    decision_question: str = ""
    candidates: list[DraftCandidate] = Field(min_length=3, max_length=3)
    warnings: list[str] = Field(default_factory=list)
    tone_guidance: str = ""


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_points: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    dates_and_deadlines: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    important_attachments: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class MessageOrderClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    span: str
    source_id: str
    reference_source_id: str
    relation: Literal["earlier", "later"]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    source_ids: list[str] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    related_source_ids: list[str] = Field(default_factory=list)
    related_attachment_ids: list[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    message_order_claims: list[MessageOrderClaim] = Field(default_factory=list)


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_type: str
    value: str
    normalized_value: str = ""
    source_excerpt: str
    confidence: float = Field(ge=0.0, le=1.0)
    important: bool


class UsageEstimate(BaseModel):
    estimated_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    allowed: bool
    reason: str = ""


class SyncProgress(BaseModel):
    account_id: str
    stage: str
    completed: int = 0
    total: int | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
