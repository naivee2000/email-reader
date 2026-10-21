from __future__ import annotations

import pytest

from email_reader.database import Database
from email_reader.usage import UsageLedger, UsageLimitError


def test_monthly_limit_is_a_hard_stop(database: Database) -> None:
    ledger = UsageLedger(database, monthly_limit_usd=0.000001)
    with pytest.raises(UsageLimitError):
        ledger.ensure_allowed("gpt-5.6-terra", 1000, 1000)

    ledger.monthly_limit_usd = 10.0
    cost = ledger.record("test", "gpt-5.6-terra", 1000, 500)
    assert cost > 0
    assert ledger.current_month_total() == pytest.approx(cost)
