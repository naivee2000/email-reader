from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from email.message import EmailMessage

from email_reader.contracts import Account, DraftReceipt, DraftRequest, ProviderKind, SyncPage
from email_reader.providers.base import (
    AuthenticationError,
    EmailProvider,
    ProviderResponseError,
    SyncCursorExpiredError,
)
from email_reader.providers.mime import parse_rfc822
from email_reader.security import SecretVault

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailProvider(EmailProvider):
    def __init__(self, account_id: str, vault: SecretVault) -> None:
        self.account_id = account_id
        self.vault = vault
        self._service = None
        self._email = ""
        self._known_message_ids: set[str] = set()
        self._known_draft_ids: set[str] = set()

    def set_known_message_ids(self, identifiers: set[str]) -> None:
        self._known_message_ids = set(identifiers)

    def set_known_draft_ids(self, identifiers: set[str]) -> None:
        self._known_draft_ids = set(identifiers)

    def draft_message_ids_are_immutable(self) -> bool:
        # Gmail draft-resource IDs persist, but each edit replaces the
        # underlying messages resource with a fresh immutable message ID.
        return True

    def _credentials(self):
        from google.oauth2.credentials import Credentials

        encoded = self.vault.get(f"gmail-token:{self.account_id}")
        if not encoded:
            return None
        return Credentials.from_authorized_user_info(json.loads(encoded), GMAIL_SCOPES)

    def _save_credentials(self, credentials) -> None:
        self.vault.set(f"gmail-token:{self.account_id}", credentials.to_json())

    def _client_config(self) -> dict:
        client_id = self.vault.get("google-client-id")
        client_secret = self.vault.get("google-client-secret")
        if not client_id or not client_secret:
            raise AuthenticationError("Google desktop OAuth client ID and secret are required")
        return {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    def _service_or_authenticate(self):
        if self._service is not None:
            return self._service
        import httplib2
        from google.auth.transport.requests import Request
        from google_auth_httplib2 import AuthorizedHttp
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        credentials = self._credentials()
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self._save_credentials(credentials)
        if not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_config(self._client_config(), GMAIL_SCOPES)
            credentials = flow.run_local_server(
                port=0, open_browser=True, access_type="offline", prompt="consent"
            )
            self._save_credentials(credentials)
        self._service = build("gmail", "v1", http=AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=30), max_refresh_attempts=1,
        ), cache_discovery=False)
        return self._service

    def authenticate(self) -> Account:
        service = self._service_or_authenticate()
        profile = service.users().getProfile(userId="me").execute()
        self._email = profile["emailAddress"]
        return Account(
            id=self.account_id,
            provider=ProviderKind.GMAIL,
            email=self._email,
            display_name=self._email.split("@", 1)[0],
            color="#EA4335",
        )

    def initial_sync(self, *, since: datetime | None, page_token: str | None = None) -> SyncPage:
        service = self._service_or_authenticate()
        state: dict[str, str] = {}
        if page_token:
            try:
                parsed = json.loads(page_token)
                state = parsed if isinstance(parsed, dict) else {"page": page_token}
            except json.JSONDecodeError:
                # Accept a page token saved by an older version of the app.
                state = {"page": page_token}
        scope = state.get("scope") or ("mail" if since and page_token is None else "all")
        if scope == "history":
            return self._history_page(state["cursor"], state.get("page"))
        if scope not in {"all", "mail", "drafts"}:
            raise ValueError("Unknown Gmail import scope")
        # Capture the mailbox watermark before listing the first page. Changes
        # during a long initial import must appear in the next history sync.
        watermark = state.get("watermark") or str(
            service.users().getProfile(userId="me").execute()["historyId"]
        )
        if scope == "drafts":
            # Search date ranges should not hide a still-editable old Draft.
            # The Draft resource lists every current underlying message ID.
            response = (
                service.users().drafts()
                .list(userId="me", pageToken=state.get("page"), maxResults=100)
                .execute()
            )
            items = []
            for draft in response.get("drafts", []):
                identifier = draft.get("message", {}).get("id")
                if not identifier:
                    raise ProviderResponseError("Gmail Draft list item has no message ID")
                items.append({"id": identifier})
        else:
            # Category exclusions concern received Inbox mail, never the
            # user's Sent/Drafts. Social/Forums are ordinary retained mail.
            query = (
                "((in:inbox -category:promotions) OR in:sent OR in:drafts)"
                if scope == "all" else
                "((in:inbox -category:promotions) OR in:sent)"
            )
            if since:
                # Gmail interprets date-form after: at midnight Pacific time.
                # Preserve the account's exact UTC cutoff, including presets
                # whose start time is not midnight. after: is exclusive.
                cutoff_second = max(0, int(since.astimezone(UTC).timestamp()) - 1)
                query += f" after:{cutoff_second}"
            response = (
                service.users().messages()
                .list(
                    userId="me", q=query, pageToken=state.get("page"), maxResults=100,
                    includeSpamTrash=False,
                )
                .execute()
            )
            items = response.get("messages", [])
        messages = []
        unchanged_labels: dict[str, list[str]] = {}
        inbox_ids: list[str] = []
        deleted_drafts: list[str] = []
        missing_ids: set[str] = set()
        draft_labels: dict[str, list[str]] = {}
        if page_token is None:
            # A full scan after an expired history cursor might not list the
            # message formerly inside a replaced/deleted Draft. Reconcile the
            # few local Draft IDs with metadata, not raw MIME. Never remove
            # their encrypted local originals.
            for identifier in sorted(self._known_draft_ids):
                try:
                    draft_labels[identifier] = self._fetch_labels(identifier)
                except Exception as exc:
                    if not self._not_found(exc):
                        raise
                    deleted_drafts.append(identifier)
                    missing_ids.add(identifier)
            unchanged_labels.update(draft_labels)
        for item in items:
            identifier = item["id"]
            if identifier in missing_ids:
                continue
            try:
                if identifier in self._known_message_ids:
                    labels = draft_labels.get(identifier)
                    if labels is None:
                        labels = self._fetch_labels(identifier)
                    # We store Gmail's underlying message ID, not the stable
                    # drafts-resource ID. Updating a draft replaces its message
                    # with a new ID; the same complete ID needs labels only.
                    unchanged_labels[identifier] = labels
                    if self._visible_inbox(labels):
                        inbox_ids.append(identifier)
                    continue
                message = self._fetch_message(identifier)
            except Exception as exc:
                if not self._not_found(exc):
                    raise
                missing_ids.add(identifier)
                continue
            messages.append(message)
            if self._visible_inbox(message.labels):
                inbox_ids.append(identifier)
        next_page = response.get("nextPageToken")
        next_state = None
        if next_page:
            next_state = {"page": next_page, "watermark": watermark}
            if scope != "all":
                next_state["scope"] = scope
        elif since and scope != "drafts":
            next_state = {"scope": "drafts", "watermark": watermark}
        return SyncPage(
            messages=messages,
            next_page_token=json.dumps(next_state, separators=(",", ":"))
            if next_state else None,
            next_cursor=watermark if not next_state else None,
            unchanged_label_states=unchanged_labels,
            inbox_scope_ids=inbox_ids,
            inbox_scope_reset=page_token is None,
            inbox_scope_complete=next_state is None,
            remote_deleted_ids=sorted(set(deleted_drafts) | missing_ids),
        )

    def incremental_sync(self, cursor: str) -> SyncPage:
        return self._history_page(cursor)

    def _history_page(self, cursor: str, page_token: str | None = None) -> SyncPage:
        service = self._service_or_authenticate()
        changed: set[str] = set()
        added_labels: dict[str, list[str]] = {}
        label_only: set[str] = set()
        deleted: set[str] = set()
        final_history_id = cursor
        try:
            response = service.users().history().list(
                userId="me", startHistoryId=cursor,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
                maxResults=50, pageToken=page_token,
            ).execute()
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise SyncCursorExpiredError(
                    "Gmail history cursor expired; a full resync is required"
                ) from exc
            raise
        for event in response.get("history", []):
            for item in event.get("messagesAdded", []):
                message = item["message"]
                changed.add(message["id"])
                if isinstance(message.get("labelIds"), list):
                    added_labels[message["id"]] = message["labelIds"]
            for key in ("labelsAdded", "labelsRemoved"):
                label_only.update(item["message"]["id"] for item in event.get(key, []))
            deleted.update(item["message"]["id"] for item in event.get("messagesDeleted", []))
        final_history_id = str(response.get("historyId", final_history_id))
        next_page = response.get("nextPageToken")
        messages = []
        unchanged_labels: dict[str, list[str]] = {}
        for message_id in sorted(changed - deleted):
            # A later label event can invalidate the snapshot embedded in a
            # messageAdded record within this same history interval.
            raw_labels = None if message_id in label_only else added_labels.get(message_id)
            # A known complete Gmail message ID identifies immutable MIME,
            # including the message inside a Draft. Ask for current labels,
            # but do not download that same body or its attachments again.
            try:
                labels = (
                    self._fetch_labels(message_id)
                    if message_id in self._known_message_ids else
                    self._normalized_labels(raw_labels)
                    if raw_labels is not None else self._fetch_labels(message_id)
                )
                if message_id in self._known_message_ids:
                    unchanged_labels[message_id] = labels
                    continue
                if self._in_import_scope(labels):
                    messages.append(self._fetch_message(message_id))
            except Exception as exc:
                if not self._not_found(exc):
                    raise
                deleted.add(message_id)
        for message_id in sorted(label_only - changed - deleted):
            try:
                if message_id not in self._known_message_ids:
                    labels = self._fetch_labels(message_id)
                    if self._in_import_scope(labels):
                        messages.append(self._fetch_message(message_id))
                    continue
                labels = self._fetch_labels(message_id)
                unchanged_labels[message_id] = labels
            except Exception as exc:
                if not self._not_found(exc):
                    raise
                deleted.add(message_id)
        return SyncPage(
            messages=messages,
            next_cursor=final_history_id if not next_page else None,
            next_page_token=json.dumps({"scope": "history", "cursor": cursor, "page": next_page},
                separators=(",", ":")) if next_page else None,
            remote_deleted_ids=sorted(deleted),
            unchanged_label_states=unchanged_labels,
        )

    @staticmethod
    def _not_found(error: Exception) -> bool:
        return getattr(getattr(error, "resp", None), "status", None) == 404

    def _fetch_labels(self, message_id: str) -> list[str]:
        response = (
            self._service_or_authenticate().users().messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=[])
            .execute()
        )
        return self._normalized_labels(response.get("labelIds", []))

    def _fetch_message(self, message_id: str):
        response = (
            self._service_or_authenticate()
            .users()
            .messages()
            .get(userId="me", id=message_id, format="raw")
            .execute()
        )
        raw = base64.urlsafe_b64decode(response["raw"] + "===")
        labels = self._normalized_labels(response.get("labelIds", []))

        return parse_rfc822(
            raw,
            provider_id=response["id"],
            provider_thread_id=response["threadId"],
            labels=labels,
            web_link=self.source_link(response["id"]),
        )

    @staticmethod
    def _normalized_labels(label_ids: list[str]) -> list[str]:
        labels = list(label_ids)
        # Gmail does not expose an ARCHIVE label. Its absence of INBOX, SENT,
        # and DRAFT means the message must not fall into our Inbox fallback.
        if not set(labels).intersection({"INBOX", "SENT", "DRAFT"}):
            labels.append("ARCHIVE")
        return labels

    @staticmethod
    def _visible_inbox(labels: list[str]) -> bool:
        label_set = set(labels)
        return "INBOX" in label_set and not label_set.intersection({
            "SENT", "DRAFT", "CATEGORY_PROMOTIONS", "SPAM", "TRASH",
        })

    @staticmethod
    def _in_import_scope(labels: list[str]) -> bool:
        label_set = set(labels)
        if label_set.intersection({"SPAM", "TRASH"}):
            return False
        if label_set.intersection({"SENT", "DRAFT"}):
            return True
        if "CATEGORY_PROMOTIONS" in label_set:
            return False
        return bool(label_set.intersection({"INBOX", "SENT", "DRAFT"}))

    def create_draft(self, request: DraftRequest) -> DraftReceipt:
        message = EmailMessage()
        message["Subject"] = request.subject
        message["To"] = ", ".join(request.to)
        if request.cc:
            message["Cc"] = ", ".join(request.cc)
        if request.bcc:
            message["Bcc"] = ", ".join(request.bcc)
        if request.in_reply_to:
            message["In-Reply-To"] = request.in_reply_to
        if request.references:
            message["References"] = " ".join(request.references)
        message.set_content(request.body_text)
        for attachment in request.attachments:
            if attachment.content is None:
                raise ValueError("A draft attachment is unavailable")
            maintype, _, subtype = attachment.mime_type.partition("/")
            message.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        payload: dict = {
            "message": {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
        }
        if request.provider_thread_id:
            payload["message"]["threadId"] = request.provider_thread_id
        response = (
            self._service_or_authenticate()
            .users()
            .drafts()
            .create(userId="me", body=payload)
            .execute()
        )
        return DraftReceipt(
            provider_draft_id=response["id"],
            provider_message_id=response["message"]["id"],
            web_link=self.source_link(response["message"]["id"]),
        )

    def source_link(self, provider_message_id: str) -> str | None:
        return f"https://mail.google.com/mail/u/{self._email or 0}/#all/{provider_message_id}"
