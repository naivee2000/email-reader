from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from email_reader.contracts import (
    Account,
    DraftReceipt,
    DraftRequest,
    SourceRetryRequest,
    SourceRetryResult,
    SyncPage,
)


class AuthenticationError(RuntimeError):
    pass


class ConsentRequiredError(AuthenticationError):
    pass


class SyncCursorExpiredError(RuntimeError):
    pass


class ProviderOperationError(RuntimeError):
    """A provider-origin failure, distinct from storage or security failures."""

    def __init__(self, message: str, *, kind: str = "connection") -> None:
        super().__init__(message)
        if kind not in {"connection", "timeout", "permission"}:
            raise ValueError("Unknown provider failure kind")
        self.kind = kind


class ProviderResponseError(ProviderOperationError, ValueError):
    """Malformed provider data; keep compatibility with validation callers."""


def provider_failure_kind(error: Exception) -> str | None:
    """Conservative isolation: unknown/local failures must stop the import.

    Do not classify plain RuntimeError/ValueError by text. A database or
    encrypted-file integrity failure must never be reported as disconnection.
    """
    if isinstance(error, AuthenticationError):
        return "permission"
    if isinstance(error, ProviderOperationError):
        return error.kind
    # Provider libraries report network failures using their own typed errors.
    import httplib2
    import httpx
    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError
    from requests.exceptions import RequestException

    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(error, (httpx.HTTPError, httplib2.HttpLib2Error, TransportError,
                          RequestException, HttpError, ConnectionError)):
        return "connection"
    if isinstance(error, RefreshError):
        return "permission"
    return None


class EmailProvider(ABC):
    """Read, synchronize, and create drafts. Deliberately has no send operation."""

    def draft_message_ids_are_immutable(self) -> bool:
        """Whether a complete Draft source can be reused by underlying message ID."""
        return False

    def set_known_message_ids(self, identifiers: set[str]) -> None:
        """Optional cache hint for providers without a native change cursor."""
        return None

    def set_known_message_revisions(self, revisions: dict[str, str]) -> None:
        """Optional cache hint to avoid refetching identical provider revisions."""
        return None

    def set_known_draft_ids(self, identifiers: set[str]) -> None:
        """Optional hint for reconciling Drafts after a provider cursor gap."""
        return None

    def set_deferred_source_ids(self, identifiers: set[str]) -> None:
        """Original retries that are not due; metadata may still be refreshed."""
        return None

    def set_excluded_message_ids(self, identifiers: set[str]) -> None:
        """Short-lived, current-policy classification cache; never guessed."""
        return None

    def retry_sources(self, requests: list[SourceRetryRequest]) -> list[SourceRetryResult]:
        """Retry incomplete originals by stable identity, independent of sync dates."""
        return []

    def recent_head(self, *, since: datetime | None) -> SyncPage:
        """A repeatable first page, without changing an import's continuation."""
        return self.initial_sync(since=since)

    @abstractmethod
    def authenticate(self) -> Account:
        raise NotImplementedError

    @abstractmethod
    def initial_sync(self, *, since: datetime | None, page_token: str | None = None) -> SyncPage:
        raise NotImplementedError

    @abstractmethod
    def incremental_sync(self, cursor: str) -> SyncPage:
        raise NotImplementedError

    @abstractmethod
    def create_draft(self, request: DraftRequest) -> DraftReceipt:
        raise NotImplementedError

    @abstractmethod
    def source_link(self, provider_message_id: str) -> str | None:
        raise NotImplementedError
