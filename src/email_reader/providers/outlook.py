from __future__ import annotations

import base64
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from email_reader.contracts import (
    Account,
    AttachmentPayload,
    DraftReceipt,
    DraftRequest,
    MessagePayload,
    ProviderKind,
    SyncPage,
)
from email_reader.providers.base import (
    AuthenticationError,
    ConsentRequiredError,
    EmailProvider,
    ProviderResponseError,
    SyncCursorExpiredError,
)
from email_reader.security import SecretVault
from email_reader.text import html_to_text

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["User.Read", "Mail.ReadWrite", "offline_access"]
MAIL_FOLDERS = (
    ("inbox", ["INBOX"]),
    ("sentitems", ["SENT"]),
    ("drafts", ["DRAFT"]),
)
MESSAGE_FIELDS = (
    "id,changeKey,conversationId,internetMessageId,subject,from,replyTo,toRecipients,ccRecipients,bccRecipients,"
    "sentDateTime,receivedDateTime,lastModifiedDateTime,body,bodyPreview,isRead,isDraft,webLink,"
    "parentFolderId,hasAttachments,inferenceClassification"
)
LIST_FIELDS = (
    "id,changeKey,lastModifiedDateTime,isRead,inferenceClassification,"
    "receivedDateTime,sentDateTime"
)


class OutlookProvider(EmailProvider):
    def __init__(self, account_id: str, vault: SecretVault) -> None:
        self.account_id = account_id
        self.vault = vault
        self._access_token: str | None = None
        self._email = ""
        self._known_revisions: dict[str, str] = {}
        self._folder_ids: dict[str, str] = {}
        self._client = httpx.Client(timeout=45.0)

    def set_known_message_revisions(self, revisions: dict[str, str]) -> None:
        self._known_revisions = dict(revisions)

    def _msal_app(self):
        import msal

        client_id = self.vault.get("microsoft-client-id")
        tenant = self.vault.get("microsoft-tenant") or "common"
        if not client_id:
            raise AuthenticationError("Microsoft public client ID is required")
        cache = msal.SerializableTokenCache()
        serialized = self.vault.get(f"outlook-token-cache:{self.account_id}")
        if serialized:
            cache.deserialize(serialized)
        app = msal.PublicClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant}",
            token_cache=cache,
        )
        return app, cache

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        app, cache = self._msal_app()
        accounts = app.get_accounts()
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0]) if accounts else None
        if not result:
            result = app.acquire_token_interactive(scopes=GRAPH_SCOPES, prompt="select_account")
        if cache.has_state_changed:
            self.vault.set(f"outlook-token-cache:{self.account_id}", cache.serialize())
        if "access_token" not in result:
            message = result.get(
                "error_description", result.get("error", "Microsoft authentication failed")
            )
            if "consent" in message.lower() or "admin" in message.lower():
                raise ConsentRequiredError(message)
            raise AuthenticationError(message)
        self._access_token = result["access_token"]
        return self._access_token

    def _request(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        if url.startswith("/"):
            url = f"{GRAPH_ROOT}{url}"
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "graph.microsoft.com"
            or not parsed.path.lower().startswith("/v1.0/")
        ):
            raise ValueError("Unexpected Microsoft Graph URL")
        method = method.upper()
        # Mail.ReadWrite is needed for drafts, but is broader than this app's
        # authority. Fail closed at the shared network boundary: never allow
        # sending, batch operations, deletes, moves or read-state changes.
        path = parsed.path
        draft_write = (
            method == "POST" and (
                path == "/v1.0/me/messages"
                or re.fullmatch(r"/v1\.0/me/messages/[^/]+/(createReply|attachments)", path)
            )
            or method == "PATCH" and re.fullmatch(r"/v1\.0/me/messages/[^/]+", path)
        )
        if method != "GET" and not draft_write:
            raise ValueError("Only read requests and draft writes are permitted")
        if method == "PATCH":
            payload = kwargs.get("json")
            editable_fields = {"subject", "body", "toRecipients", "ccRecipients", "bccRecipients"}
            if not isinstance(payload, dict) or not payload or set(payload) - editable_fields:
                raise ValueError("Only reviewed draft fields may be updated")
        refreshed_token = False
        throttle_retries = 0
        throttle_wait = 0.0
        while True:
            response = self._client.request(method, url, headers={
                "Authorization": f"Bearer {self._token()}", "Accept": "application/json"
            }, **kwargs)
            if response.status_code == 401 and not refreshed_token:
                # An access token may expire between calls. Refresh once through
                # MSAL, then retry the same request; never loop on revoked access.
                self._access_token = None
                refreshed_token = True
                continue
            if response.status_code == 429 and throttle_retries < 3:
                # Graph did not accept a throttled operation. Respect its
                # Retry-After when feasible; otherwise leave the sync cursor
                # unchanged and let the next refresh resume this page. Never
                # retry ambiguous POST failures such as a 503 or timeout.
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = float(2**throttle_retries)
                if not math.isfinite(delay) or delay < 0:
                    delay = float(2**throttle_retries)
                if throttle_wait + delay > 30:
                    response.raise_for_status()
                time.sleep(delay)
                throttle_wait += delay
                throttle_retries += 1
                continue
            response.raise_for_status()
            return response.json() if response.content else {}

    def authenticate(self) -> Account:
        profile = self._request("GET", "/me?$select=displayName,mail,userPrincipalName")
        self._email = profile.get("mail") or profile["userPrincipalName"]
        return Account(
            id=self.account_id,
            provider=ProviderKind.OUTLOOK,
            email=self._email,
            display_name=profile.get("displayName", ""),
            color="#0078D4",
        )

    def initial_sync(self, *, since: datetime | None, page_token: str | None = None) -> SyncPage:
        if page_token:
            try:
                state = json.loads(page_token)
            except json.JSONDecodeError:
                state = {"folder": 0, "url": page_token}
        else:
            state = {"folder": 0}
        if state.get("mode") == "delta":
            return self._delta_page(state)
        # Keep an import-start watermark for the one-time metadata-only delta
        # bootstrap; changes made during a long import must be read afterward.
        watermark = state.get("watermark") or datetime.now(UTC).isoformat()
        folder_index = int(state.get("folder", 0))
        folder_name, labels = MAIL_FOLDERS[folder_index]
        url = state.get("url") or self._folder_url(folder_name, since=since)
        response = self._request("GET", url)
        values = response.get("value", [])
        if not isinstance(values, list):
            raise ProviderResponseError("Outlook folder page is invalid")
        messages: list[MessagePayload] = []
        removed: list[str] = []
        filtered: list[str] = []
        inbox_ids: list[str] = []
        unchanged_read_states: dict[str, bool] = {}
        unchanged_label_states: dict[str, list[str]] = {}
        for item in values:
            identifier = item.get("id")
            if not identifier:
                raise ProviderResponseError("Outlook folder item has no ID")
            revision = item.get("changeKey")
            if revision and self._known_revisions.get(identifier) == revision:
                if isinstance(item.get("isRead"), bool):
                    unchanged_read_states[identifier] = not item["isRead"]
                unchanged_label_states[identifier] = labels
                if "INBOX" in labels:
                    inbox_ids.append(identifier)
                continue
            try:
                full = self._request(
                    "GET", f"/me/messages/{quote(identifier, safe='')}?$select={MESSAGE_FIELDS}"
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                removed.append(identifier)
                continue
            message = self._message(full, labels)
            if message is not None:
                messages.append(message)
                if message.provider_revision:
                    self._known_revisions[identifier] = message.provider_revision
                if "INBOX" in labels:
                    inbox_ids.append(identifier)
            else:
                filtered.append(identifier)
        next_link = response.get("@odata.nextLink")
        cursor = None
        if next_link:
            next_token = json.dumps(
                {"folder": folder_index, "url": next_link, "watermark": watermark},
                separators=(",", ":"),
            )
        elif folder_index + 1 < len(MAIL_FOLDERS):
            next_token = json.dumps(
                {"folder": folder_index + 1, "watermark": watermark},
                separators=(",", ":"),
            )
        else:
            next_token = None
            cursor = json.dumps({"since": watermark}, separators=(",", ":"))
        return SyncPage(
            messages=messages, remote_deleted_ids=removed,
            filtered_bulk_ids=filtered, unchanged_read_states=unchanged_read_states,
            unchanged_label_states=unchanged_label_states,
            labels_include_read_state=False,
            inbox_scope_ids=inbox_ids, inbox_scope_reset=page_token is None,
            inbox_scope_complete=not bool(next_token),
            next_page_token=next_token, next_cursor=cursor,
            examined_count=len(values),
        )

    def incremental_sync(self, cursor: str) -> SyncPage:
        state = json.loads(cursor)
        if state.get("mode") == "delta":
            links = state.get("links")
            if not isinstance(links, dict) or not all(folder in links for folder, _ in MAIL_FOLDERS):
                raise ValueError("Outlook delta cursor is incomplete")
            return self._delta_page({"mode": "delta", "folder": 0, "links": links})
        # Older cursors predate Graph delta. Bootstrap each folder with metadata
        # only; fetch full content solely for items modified since that cursor.
        since = state.get("since")
        if not isinstance(since, str) or not since:
            raise ValueError("Outlook sync cursor is invalid")
        return self._delta_page({"mode": "delta", "folder": 0, "links": {}, "since": since})

    def _delta_page(self, state: dict[str, Any]) -> SyncPage:
        folder_index = int(state.get("folder", 0))
        if not 0 <= folder_index < len(MAIL_FOLDERS):
            raise ValueError("Outlook delta folder is invalid")
        folder_name, labels = MAIL_FOLDERS[folder_index]
        links = dict(state.get("links") or {})
        since = state.get("since")
        url = state.get("url") or links.get(folder_name) or self._delta_url(folder_name)
        try:
            response = self._request("GET", url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 410:
                raise SyncCursorExpiredError(
                    "Outlook change cursor expired; a full resync is required"
                ) from exc
            raise
        values = response.get("value", [])
        if not isinstance(values, list):
            raise ProviderResponseError("Outlook delta page is invalid")
        messages: list[MessagePayload] = []
        removed: list[str] = []
        unchanged_revisions: dict[str, str] = {}
        unchanged_read_states: dict[str, bool] = {}
        unchanged_label_states: dict[str, list[str]] = {}
        filtered: list[str] = []
        removed_folder_roles: dict[str, str] = {}
        for item in values:
            identifier = item.get("id")
            if not identifier:
                raise ProviderResponseError("Outlook delta item has no ID")
            if "@removed" in item:
                # Graph delta is folder-scoped: a move out of this folder
                # emits the same @removed reason as a mailbox deletion.
                # Check the global message with metadata only before changing
                # source availability or visible folder state.
                try:
                    current = self._request(
                        "GET", f"/me/messages/{quote(identifier, safe='')}?"
                        "$select=id,parentFolderId"
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    removed.append(identifier)
                    self._known_revisions.pop(identifier, None)
                    continue
                if current.get("id") != identifier or not current.get("parentFolderId"):
                    raise ProviderResponseError("Outlook removal metadata is incomplete")
                if current["parentFolderId"] != self._folder_id(folder_name):
                    removed_folder_roles[identifier] = folder_name
                continue
            revision = item.get("changeKey")
            if revision and self._known_revisions.get(identifier) == revision:
                if isinstance(item.get("isRead"), bool):
                    unchanged_read_states[identifier] = not item["isRead"]
                unchanged_label_states[identifier] = labels
                continue
            if since and folder_name not in links:
                modified = item.get("lastModifiedDateTime")
                if not modified:
                    raise ProviderResponseError("Outlook delta item has no modification time")
                modified_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                since_at = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if modified_at.tzinfo is None or since_at.tzinfo is None:
                    raise ProviderResponseError("Outlook delta times must include a timezone")
                if modified_at <= since_at and identifier in self._known_revisions:
                    if revision:
                        unchanged_revisions[identifier] = revision
                        self._known_revisions[identifier] = revision
                    if isinstance(item.get("isRead"), bool):
                        unchanged_read_states[identifier] = not item["isRead"]
                    unchanged_label_states[identifier] = labels
                    continue
            try:
                full = self._request(
                    "GET", f"/me/messages/{quote(identifier, safe='')}?$select={MESSAGE_FIELDS}"
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                removed.append(identifier)
                continue
            message = self._message(full, labels)
            if message is not None:
                messages.append(message)
                if message.provider_revision:
                    self._known_revisions[identifier] = message.provider_revision
            else:
                filtered.append(identifier)
        next_link = response.get("@odata.nextLink")
        delta_link = response.get("@odata.deltaLink")
        if bool(next_link) == bool(delta_link):
            raise ProviderResponseError("Outlook delta page needs exactly one continuation link")
        if next_link:
            next_state = {"mode": "delta", "folder": folder_index, "links": links,
                          "url": next_link}
            if since:
                next_state["since"] = since
            return SyncPage(
                messages=messages, remote_deleted_ids=removed,
                unchanged_revisions=unchanged_revisions,
                unchanged_read_states=unchanged_read_states,
                unchanged_label_states=unchanged_label_states,
                labels_include_read_state=False,
                filtered_bulk_ids=filtered,
                removed_folder_roles=removed_folder_roles,
                next_page_token=json.dumps(next_state, separators=(",", ":")),
                examined_count=len(values),
            )
        links[folder_name] = delta_link
        if folder_index + 1 < len(MAIL_FOLDERS):
            next_state = {"mode": "delta", "folder": folder_index + 1, "links": links}
            if since:
                next_state["since"] = since
            return SyncPage(
                messages=messages, remote_deleted_ids=removed,
                unchanged_revisions=unchanged_revisions,
                unchanged_read_states=unchanged_read_states,
                unchanged_label_states=unchanged_label_states,
                labels_include_read_state=False,
                filtered_bulk_ids=filtered,
                removed_folder_roles=removed_folder_roles,
                next_page_token=json.dumps(next_state, separators=(",", ":")),
                examined_count=len(values),
            )
        return SyncPage(
            messages=messages, remote_deleted_ids=removed,
            unchanged_revisions=unchanged_revisions,
            unchanged_read_states=unchanged_read_states,
            unchanged_label_states=unchanged_label_states,
            labels_include_read_state=False,
            filtered_bulk_ids=filtered,
            removed_folder_roles=removed_folder_roles,
            next_cursor=json.dumps({"mode": "delta", "links": links}, separators=(",", ":")),
            examined_count=len(values),
        )

    @staticmethod
    def _delta_url(folder_name: str) -> str:
        return (
            f"/me/mailFolders/{folder_name}/messages/delta?"
            "$select=id,lastModifiedDateTime,changeKey,isRead,inferenceClassification"
        )

    def _folder_id(self, folder_name: str) -> str:
        if folder_name not in self._folder_ids:
            response = self._request(
                "GET", f"/me/mailFolders/{folder_name}?$select=id"
            )
            identifier = response.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ProviderResponseError("Outlook folder identity is unavailable")
            self._folder_ids[folder_name] = identifier
        return self._folder_ids[folder_name]

    @staticmethod
    def _folder_url(
        folder_name: str,
        *,
        since: datetime | None = None,
    ) -> str:
        url = f"/me/mailFolders/{folder_name}/messages?$top=100&$select={LIST_FIELDS}"
        # An unsent Draft has no sent time and need not have a received time.
        # Import all current Drafts, as the Apple Mail route does, then use
        # Graph's changeKey/delta cursor to avoid re-downloading unchanged ones.
        if folder_name == "drafts":
            return url
        date_field = "sentDateTime" if folder_name == "sentitems" else "receivedDateTime"
        if since:
            stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            url += f"&$filter={date_field} ge {stamp}"
            url += f"&$orderby={date_field} desc"
        else:
            url += f"&$orderby={date_field} desc"
        return url

    def _message(
        self, item: dict[str, Any], labels: list[str] | None = None
    ) -> MessagePayload | None:
        labels = list(labels or [])
        # Outlook's Other is a focus preference, not a Promotions category.
        # Unknown classifications must remain available to the user.
        sender = item.get("from", {}).get("emailAddress", {})
        body_html = (
            item.get("body", {}).get("content", "")
            if item.get("body", {}).get("contentType") == "html"
            else ""
        )
        body_text = (
            item.get("body", {}).get("content", "") if not body_html else html_to_text(body_html)
        )
        attachments: list[AttachmentPayload] = []
        # Graph's hasAttachments excludes inline-only attachments.
        if item.get("hasAttachments") or "cid:" in item.get("body", {}).get("content", "").lower():
            url: str | None = f"/me/messages/{quote(item['id'], safe='')}/attachments?$top=100"
            seen_pages: set[str] = set()
            while url:
                if url in seen_pages:
                    raise ProviderResponseError("Repeated Microsoft Graph attachment page")
                seen_pages.add(url)
                response = self._request("GET", url)
                for attachment in response.get("value", []):
                    encoded = attachment.get("contentBytes")
                    content = base64.b64decode(encoded) if encoded else None
                    attachments.append(
                        AttachmentPayload(
                            provider_id=attachment["id"],
                            filename=attachment.get("name", "attachment"),
                            mime_type=attachment.get("contentType", "application/octet-stream"),
                            size=attachment.get("size", len(content or b"")),
                            content=content,
                            is_inline=attachment.get("isInline", False),
                            content_id=attachment.get("contentId") or "",
                            content_location=attachment.get("contentLocation") or "",
                        )
                    )
                url = response.get("@odata.nextLink")
        return MessagePayload(
            provider_id=item["id"],
            provider_revision=item.get("changeKey"),
            provider_thread_id=item.get("conversationId") or item["id"],
            internet_message_id=item.get("internetMessageId"),
            subject=item.get("subject") or "(no subject)",
            sender_name=sender.get("name", ""),
            sender_email=sender.get("address", ""),
            reply_to=self._addresses(item.get("replyTo", [])),
            to=self._addresses(item.get("toRecipients", [])),
            cc=self._addresses(item.get("ccRecipients", [])),
            bcc=self._addresses(item.get("bccRecipients", [])),
            sent_at=self._datetime(item.get("sentDateTime")),
            received_at=self._datetime(item.get("receivedDateTime")),
            body_text=body_text,
            body_html=body_html,
            snippet=item.get("bodyPreview", ""),
            labels=labels,
            is_sent="SENT" in labels
            or (sender.get("address", "").lower() == self._email.lower() if self._email else False),
            is_draft="DRAFT" in labels or item.get("isDraft", False),
            is_unread=not item.get("isRead", True),
            web_link=item.get("webLink"),
            attachments=attachments,
        )

    @staticmethod
    def _addresses(recipients: list[dict[str, Any]]) -> list[str]:
        return [
            item.get("emailAddress", {}).get("address", "")
            for item in recipients
            if item.get("emailAddress")
        ]

    @staticmethod
    def _datetime(value: str | None) -> datetime:
        if not value:
            return datetime.now(UTC)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

    def create_draft(self, request: DraftRequest) -> DraftReceipt:
        if any(
            item.content is None or len(item.content) >= 3 * 1024 * 1024
            for item in request.attachments
        ):
            raise ValueError("Graph draft attachments must be available and smaller than 3 MB")
        message = {
            "subject": request.subject,
            "body": {"contentType": "Text", "content": request.body_text},
            "toRecipients": self._recipient_payload(request.to),
            "ccRecipients": self._recipient_payload(request.cc),
            "bccRecipients": self._recipient_payload(request.bcc),
        }
        if request.reply_to_provider_message_id:
            draft = self._request(
                "POST", f"/me/messages/{quote(request.reply_to_provider_message_id, safe='')}/createReply"
            )
            response = self._request("PATCH", f"/me/messages/{quote(draft['id'], safe='')}", json=message)
        else:
            response = self._request("POST", "/me/messages", json=message)
        for attachment in request.attachments:
            self._request(
                "POST",
                f"/me/messages/{quote(response['id'], safe='')}/attachments",
                json={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment.filename,
                    "contentType": attachment.mime_type,
                    "contentBytes": base64.b64encode(attachment.content).decode("ascii"),
                },
            )
        return DraftReceipt(
            provider_draft_id=response["id"],
            provider_message_id=response["id"],
            web_link=response.get("webLink"),
        )

    @staticmethod
    def _recipient_payload(addresses: list[str]) -> list[dict[str, dict[str, str]]]:
        return [{"emailAddress": {"address": address}} for address in addresses]

    def source_link(self, provider_message_id: str) -> str | None:
        try:
            response = self._request("GET", f"/me/messages/{provider_message_id}?$select=webLink")
            return response.get("webLink")
        except Exception:
            return None
