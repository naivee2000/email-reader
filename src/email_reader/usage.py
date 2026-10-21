from __future__ import annotations

from datetime import UTC, datetime

from email_reader.contracts import UsageEstimate
from email_reader.database import Database

PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.4-nano": (0.20, 1.25),
    "text-embedding-3-small": (0.02, 0.0),
}


class UsageLimitError(RuntimeError):
    pass


class UsageLedger:
    def __init__(self, database: Database, monthly_limit_usd: float) -> None:
        self.db = database
        self.monthly_limit_usd = monthly_limit_usd

    def current_month_total(self) -> float:
        month = datetime.now(UTC).strftime("%Y-%m")
        return float(
            self.db.scalar(
                "SELECT COALESCE(SUM(estimated_usd),0) FROM usage_records WHERE occurred_at LIKE ?",
                (f"{month}%",),
                0.0,
            )
        )

    def estimate(self, model: str, input_tokens: int, output_tokens: int = 0) -> UsageEstimate:
        input_price, output_price = PRICES_PER_MILLION.get(model, (5.0, 20.0))
        cost = input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price
        remaining = self.monthly_limit_usd - self.current_month_total()
        return UsageEstimate(
            estimated_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            allowed=cost <= remaining,
            reason="" if cost <= remaining else "Monthly AI usage limit would be exceeded",
        )

    def ensure_allowed(
        self, model: str, input_tokens: int, output_tokens: int = 0
    ) -> UsageEstimate:
        estimate = self.estimate(model, input_tokens, output_tokens)
        if not estimate.allowed:
            raise UsageLimitError(estimate.reason)
        return estimate

    def record(self, operation: str, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = self.estimate(model, input_tokens, output_tokens).estimated_usd
        self._insert(operation, model, input_tokens, output_tokens, cost)
        return cost

    def record_local(
        self, operation: str, model: str, input_tokens: int, output_tokens: int
    ) -> None:
        self._insert(operation, f"codex:{model}", input_tokens, output_tokens, 0.0)

    def _insert(
        self,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_usd: float,
    ) -> None:
        self.db.execute(
            """INSERT INTO usage_records(occurred_at,operation,model,input_tokens,output_tokens,estimated_usd)
               VALUES(?,?,?,?,?,?)""",
            (self.db.now(), operation, model, input_tokens, output_tokens, estimated_usd),
        )
