"""Small, resumable provider turns. Never run concurrent Apple Mail commands."""
from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime, timedelta

from email_reader.categories import IMPORT_POLICY_VERSION
from email_reader.providers.apple_mail import AppleMailProvider
from email_reader.providers.base import provider_failure_kind


class RefreshRound:
    def __init__(self, context, provider_for, *, accounts, schedule, automatic=False,
                 force_drafts=False, invalidate_provider=None):
        self.context = context
        self.provider_for = provider_for
        self.schedule = schedule
        self.invalidate_provider = invalidate_provider
        now = time.monotonic()
        self._all_accounts = {a["id"]: a for a in accounts}
        self.accounts = {a["id"]: a for a in accounts
                         if not automatic or schedule.get(a["id"], {}).get("due", 0) <= now}
        self.pending = deque((identifier, "incremental") for identifier in self.accounts)
        self.repair = deque((identifier, "repair") for identifier in self.accounts)
        self.heads = deque(self.accounts)
        self.last_head = {identifier: now for identifier in self.accounts}
        self.failed = {}
        self._completed_incremental = set()
        self._completed_repair = set()
        self._retrying = set()
        self.processed = 0
        self.examined = 0
        self.force_drafts = force_drafts
        self.started = now

    def _enqueue_account(self, identifier, now):
        """Resume only unfinished stages, always checking the new head first."""
        self.accounts[identifier] = self._all_accounts[identifier]
        if identifier not in self.heads:
            self.heads.append(identifier)
        self.last_head.setdefault(identifier, now)
        if identifier not in self._completed_incremental and (identifier, "incremental") not in self.pending:
            self.pending.append((identifier, "incremental"))
        if identifier not in self._completed_repair and (identifier, "repair") not in self.repair:
            self.repair.append((identifier, "repair"))

    def _wake_due_accounts(self, now):
        # A round can spend a long time repairing another account. Backoff is
        # an account deadline, not a ban lasting until that whole round ends.
        for identifier in self._all_accounts:
            if self.schedule.get(identifier, {}).get("due", 0) > now:
                continue
            if identifier not in self.accounts:
                self._enqueue_account(identifier, now)
            elif identifier in self.failed:
                self.failed.pop(identifier)
                self._retrying.add(identifier)
                self._enqueue_account(identifier, now)

    def _provider(self, account_id, *, repair=False):
        provider = self.provider_for(account_id)
        if isinstance(provider, AppleMailProvider):
            # Drafts are checked in the background, without holding up other accounts.
            provider.include_drafts = self.force_drafts or repair or not self.accounts[account_id].get("sync_cursor")
        return provider

    def step(self, report):
        if self.context.sync.paused:
            return {"done": True, "paused": True, "processed": self.processed}
        now = time.monotonic()
        self._wake_due_accounts(now)
        # A long scan/repair must not prevent a fresh first-page check every five minutes.
        for identifier in self.accounts:
            if identifier not in self.failed and now - self.last_head[identifier] >= 300 and identifier not in self.heads:
                self.heads.append(identifier)
        if self.heads:
            identifier, kind = self.heads.popleft(), "freshness"
            self.last_head[identifier] = now
        elif self.pending:
            identifier, kind = self.pending.popleft()
        elif self.repair:
            identifier, kind = self.repair.popleft()
        else:
            return {"done": True, "processed": self.processed, "failed": dict(self.failed)}
        if identifier in self.failed:
            return {"done": False, "processed": self.processed}
        account = self.accounts[identifier]
        since = datetime.fromisoformat(account["sync_from"]) if account["sync_from"] else None
        version = IMPORT_POLICY_VERSION
        if kind == "freshness":
            since = max(since or datetime.min.replace(tzinfo=UTC), datetime.now(UTC) - timedelta(days=7))
        report({"stage": "Checking newest mail" if kind == "freshness" else
                "Repairing older import coverage" if kind == "repair" else "Refreshing mail",
                "detail": account["email"], "items": self.examined})
        try:
            if kind == "originals":
                recovered = self.context.sync.retry_pending_originals(identifier, self._provider(identifier), limit=1)
                self.processed += recovered
                return {"done": False, "updated": recovered, "processed": self.processed}
            result = self.context.sync.sync_account_step(identifier, self._provider(identifier, repair=kind == "repair"),
                since=since, job_kind=kind, policy_version=version)
        except Exception as error:
            # Only recognized provider failures can be isolated. A plain
            # ValueError/RuntimeError may be integrity, disk or shutdown state.
            code = provider_failure_kind(error)
            if code is None:
                raise
            self.failed[identifier] = code
            prior = self.schedule.get(identifier, {}).get("failures", 0) + 1
            self.schedule[identifier] = {"failures": prior,
                                         "due": time.monotonic() + min(3600, 300 * 2 ** min(prior - 1, 4)),
                                         "error": code}
            if self.invalidate_provider is not None:
                self.invalidate_provider(identifier)
            return {"done": False, "account": identifier, "error": code, "processed": self.processed}
        self.processed += result.processed
        self.examined += result.examined
        if kind == "freshness":
            self._retrying.discard(identifier)
            if identifier in self._completed_incremental:
                self.schedule[identifier] = {"failures": 0, "due": time.monotonic() + 300, "error": ""}
        if kind == "incremental" and result.complete:
            self._completed_incremental.add(identifier)
            self.schedule[identifier] = {"failures": 0, "due": time.monotonic() + 300, "error": ""}
            self.pending.append((identifier, "originals"))
        elif kind == "incremental" and not result.paused:
            self.pending.append((identifier, kind))
        elif kind == "repair" and not result.complete and not result.paused:
            self.repair.append((identifier, kind))
        elif kind == "repair" and result.complete:
            self._completed_repair.add(identifier)
        return {"done": bool(result.paused), "paused": result.paused, "processed": self.processed,
                "examined": self.examined, "updated": result.processed, "account": identifier,
                "kind": kind, "account_complete": result.complete}
