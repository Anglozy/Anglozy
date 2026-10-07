"""Daily loss guard for prop-firm drawdown rules (FundedNext style).

The question it answers before every new trade: *if every open position, every
pending order and this new trade all hit their stop loss, would today's loss
exceed the limit?* If yes, the trade is refused.

* Start-of-day balance = current balance minus today's realised P&L (from
  MT5 deal history), so the baseline survives bot restarts.
* Today's loss counts floating P&L (equity), not just closed trades.
* A position without a stop loss has unbounded risk, so no new trades are
  allowed while one is open.
* Optional overall floor: equity at worst case must stay above
  ``initial_balance * (1 - max_total_loss_percent / 100)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountRisk:
    balance: float
    equity: float
    realized_today: float  # closed P&L since the start of the trading day (incl. commission/swap)
    open_risk: float  # money lost if every open position and pending order hits its stop
    unprotected: tuple[int, ...] = ()  # tickets without a stop loss
    currency: str = "USD"

    @property
    def day_start_balance(self) -> float:
        return self.balance - self.realized_today

    @property
    def day_loss(self) -> float:
        """Loss so far today (positive = losing), including floating P&L."""
        return self.day_start_balance - self.equity


@dataclass(frozen=True)
class DailyLossDecision:
    blocked: bool
    reason: str
    day_start_balance: float
    day_loss: float
    limit_amount: float
    worst_case_loss: float
    details: dict = field(default_factory=dict)

    @property
    def used_percent(self) -> float:
        return 100 * max(self.day_loss, 0.0) / self.day_start_balance if self.day_start_balance else 0.0


class DailyLossGuard:
    def __init__(
        self,
        limit_percent: float = 4.0,
        max_total_loss_percent: float | None = None,
        initial_balance: float | None = None,
    ) -> None:
        if not 0 < limit_percent <= 50:
            raise ValueError("limit_percent must be in (0, 50]")
        if max_total_loss_percent is not None and not initial_balance:
            raise ValueError("initial_balance is required with max_total_loss_percent")
        self.limit_percent = limit_percent
        self.max_total_loss_percent = max_total_loss_percent
        self.initial_balance = initial_balance

    def check(self, acct: AccountRisk, new_trade_risk: float = 0.0) -> DailyLossDecision:
        start = acct.day_start_balance
        limit = start * self.limit_percent / 100
        loss = acct.day_loss
        worst = loss + acct.open_risk + new_trade_risk
        cur = acct.currency

        def decide(blocked: bool, reason: str, **details) -> DailyLossDecision:
            return DailyLossDecision(blocked, reason, start, loss, limit, worst, details)

        if loss >= limit:
            return decide(True, f"Daily loss limit reached: down {loss:,.2f} {cur} today "
                                f"(limit {self.limit_percent:g}% = {limit:,.2f}). Trading halted until the next day.")
        if acct.unprotected:
            return decide(True, f"Open position(s) without a stop loss ({', '.join(map(str, acct.unprotected))}); "
                                "risk cannot be bounded, no new trades.")
        if worst > limit:
            return decide(True, f"Trade refused: if all stops hit, today's loss would be {worst:,.2f} {cur}, "
                                f"over the {self.limit_percent:g}% daily limit ({limit:,.2f}).",
                          headroom=limit - loss - acct.open_risk)

        if self.max_total_loss_percent is not None:
            floor = self.initial_balance * (1 - self.max_total_loss_percent / 100)
            worst_equity = acct.equity - acct.open_risk - new_trade_risk
            if worst_equity < floor:
                return decide(True, f"Trade refused: worst-case equity {worst_equity:,.2f} {cur} would breach the "
                                    f"max-loss floor {floor:,.2f} ({self.max_total_loss_percent:g}% of "
                                    f"{self.initial_balance:,.2f}).", floor=floor)

        return decide(False, f"Daily loss {max(loss, 0):,.2f} of {limit:,.2f} {cur} limit; "
                             f"worst case with open risk {worst:,.2f}",
                      headroom=limit - loss - acct.open_risk)
