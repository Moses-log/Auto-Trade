from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Serialises all load-modify-save sequences across threads
investors_lock = threading.Lock()

_REPO_FILE = Path(__file__).parent.parent / "investors.json"
INVESTORS_FILE = Path(os.getenv("INVESTORS_PATH", str(_REPO_FILE)))


@dataclass
class Deposit:
    amount: float
    entry_spy: float
    date: str


@dataclass
class Investor:
    name: str
    deposits: list[Deposit] = field(default_factory=list)


def load_investors(path: Path = INVESTORS_FILE) -> list[Investor]:
    if not path.exists():
        if _REPO_FILE != path and _REPO_FILE.exists():
            try:
                path.write_text(_REPO_FILE.read_text(encoding="utf-8-sig"), encoding="utf-8")
                log.info("Migrated investors.json to %s", path)
            except Exception as exc:
                log.warning("investors.json migration failed: %s", exc)
        if not path.exists():
            return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return [
            Investor(
                name=inv["name"],
                deposits=[Deposit(**d) for d in inv["deposits"]],
            )
            for inv in data["investors"]
        ]
    except Exception as exc:
        raise ValueError(f"investors.json is malformed: {exc}") from exc


def serialize_investors(investors: list[Investor]) -> str:
    data = {
        "investors": [
            {
                "name": inv.name,
                "deposits": [
                    {"amount": d.amount, "entry_spy": d.entry_spy, "date": d.date}
                    for d in inv.deposits
                ],
            }
            for inv in investors
        ]
    }
    return json.dumps(data, indent=2)


def save_investors(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
    path.write_text(serialize_investors(investors), encoding="utf-8")


def get_total_deposited(investor: Investor) -> float:
    return sum(d.amount for d in investor.deposits)


@dataclass
class InvestorResult:
    name: str
    total_deposited: float
    current_equity: float
    dollar_pnl: float
    pct_pnl: float
    portfolio_share: float


@dataclass
class InvestorBreakdown:
    investors: list[InvestorResult]
    spy_price: float
    total_portfolio: float
    total_deposited: float
    overall_dollar_pnl: float
    overall_pct_pnl: float


def compute_breakdown(investors: list[Investor], spy_price: float) -> InvestorBreakdown:
    results: list[InvestorResult] = []
    for inv in investors:
        total_deposited = sum(d.amount for d in inv.deposits)
        current_equity = sum(d.amount * spy_price / d.entry_spy for d in inv.deposits if d.entry_spy)
        dollar_pnl = current_equity - total_deposited
        pct_pnl = (dollar_pnl / total_deposited * 100) if total_deposited else 0.0
        results.append(
            InvestorResult(
                name=inv.name,
                total_deposited=total_deposited,
                current_equity=current_equity,
                dollar_pnl=dollar_pnl,
                pct_pnl=pct_pnl,
                portfolio_share=0.0,
            )
        )

    total_portfolio = sum(r.current_equity for r in results)
    for r in results:
        r.portfolio_share = (r.current_equity / total_portfolio * 100) if total_portfolio else 0.0

    total_deposited = sum(r.total_deposited for r in results)
    overall_dollar_pnl = total_portfolio - total_deposited
    overall_pct_pnl = (overall_dollar_pnl / total_deposited * 100) if total_deposited else 0.0

    return InvestorBreakdown(
        investors=results,
        spy_price=spy_price,
        total_portfolio=total_portfolio,
        total_deposited=total_deposited,
        overall_dollar_pnl=overall_dollar_pnl,
        overall_pct_pnl=overall_pct_pnl,
    )


def format_discord_message(breakdown: InvestorBreakdown, date_str: str) -> str:
    lines = [
        f"📊 **Investor Breakdown — {date_str}**",
        f"SPY: ${breakdown.spy_price:,.2f}",
        "",
    ]
    for r in breakdown.investors:
        if r.dollar_pnl >= 0:
            pnl_str = f"+${r.dollar_pnl:,.2f} (+{r.pct_pnl:.2f}%)"
        else:
            pnl_str = f"-${abs(r.dollar_pnl):,.2f} ({r.pct_pnl:.2f}%)"
        lines += [
            f"**{r.name}**",
            f"> Deposited: ${r.total_deposited:,.2f}",
            f"> Current Equity: ${r.current_equity:,.2f}",
            f"> P&L: {pnl_str}",
            f"> Portfolio Share: {r.portfolio_share:.1f}%",
            "",
        ]
    if breakdown.overall_dollar_pnl >= 0:
        overall_pnl_str = f"+${breakdown.overall_dollar_pnl:,.2f} (+{breakdown.overall_pct_pnl:.2f}%)"
    else:
        overall_pnl_str = f"-${abs(breakdown.overall_dollar_pnl):,.2f} ({breakdown.overall_pct_pnl:.2f}%)"
    lines += [
        "─" * 25,
        f"**Total Portfolio: ${breakdown.total_portfolio:,.2f}**",
        f"**Total Deposited: ${breakdown.total_deposited:,.2f}**",
        f"**Overall P&L: {overall_pnl_str}**",
    ]
    return "\n".join(lines)
