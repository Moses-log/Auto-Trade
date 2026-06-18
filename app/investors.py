from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path

import pytz

log = logging.getLogger(__name__)

# Serialises all load-modify-save sequences across async coroutines
investors_lock = asyncio.Lock()

_ET = pytz.timezone("America/New_York")
_REPO_FILE = Path(__file__).parent.parent / "investors.json"
INVESTORS_FILE = Path(os.getenv("INVESTORS_PATH", str(_REPO_FILE)))


@dataclass
class Deposit:
    amount: float
    entry_spy: float
    date: str


@dataclass
class Withdrawal:
    units: float        # SPY units redeemed (= proceeds / exit_spy)
    exit_spy: float     # SPY price at redemption
    cost_basis: float   # FIFO cost of the redeemed units
    proceeds: float     # cash paid out (units * exit_spy)
    date: str           # ISO date


@dataclass
class Investor:
    name: str
    deposits: list[Deposit] = field(default_factory=list)
    withdrawals: list[Withdrawal] = field(default_factory=list)


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
                # backwards-compatible: existing records have no withdrawals field
                withdrawals=[Withdrawal(**w) for w in inv.get("withdrawals", [])],
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
                "withdrawals": [
                    {
                        "units": w.units,
                        "exit_spy": w.exit_spy,
                        "cost_basis": w.cost_basis,
                        "proceeds": w.proceeds,
                        "date": w.date,
                    }
                    for w in inv.withdrawals
                ],
            }
            for inv in investors
        ]
    }
    return json.dumps(data, indent=2)


def save_investors(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(serialize_investors(investors), encoding="utf-8")
    tmp.replace(path)


def get_total_deposited(investor: Investor) -> float:
    return sum(d.amount for d in investor.deposits)


def get_deposit_events() -> list[tuple[str, float]]:
    """Return [(iso_date, amount), ...] from all investors, sorted ascending.

    Used by P&L modules to strip out external cash injections when computing
    deposit-adjusted returns (prevents new capital from inflating P&L charts).
    """
    investors = load_investors()
    events = [(d.date, d.amount) for inv in investors for d in inv.deposits]
    return sorted(events)


def _net_units(investor: Investor) -> float:
    """SPY units the investor currently holds (deposits minus all redemptions)."""
    deposit_units = sum(d.amount / d.entry_spy for d in investor.deposits if d.entry_spy)
    withdrawn_units = sum(w.units for w in investor.withdrawals)
    return deposit_units - withdrawn_units


def compute_time_weighted_capital(investor: Investor, year: int) -> float:
    """Average dollar capital `investor` had in the fund during `year`.

    Deposits add capital on their date; withdrawals (at cost basis) reduce it.
    Used to split the year's realized gains fairly among investors.
    """
    year_start = _date(year, 1, 1)
    year_end = _date(year + 1, 1, 1)
    total_days = (year_end - year_start).days

    balance = 0.0
    events: dict[_date, float] = {}

    for d in investor.deposits:
        try:
            d_date = _date.fromisoformat(d.date)
        except (ValueError, TypeError):
            continue
        if d_date < year_start:
            balance += d.amount
        elif d_date < year_end:
            events[d_date] = events.get(d_date, 0.0) + d.amount

    for w in investor.withdrawals:
        try:
            w_date = _date.fromisoformat(w.date)
        except (ValueError, TypeError):
            continue
        if w_date < year_start:
            balance -= w.cost_basis
        elif w_date < year_end:
            events[w_date] = events.get(w_date, 0.0) - w.cost_basis

    weighted_sum = 0.0
    current = year_start
    for event_date in sorted(events):
        weighted_sum += max(balance, 0.0) * (event_date - current).days
        balance += events[event_date]
        current = event_date

    weighted_sum += max(balance, 0.0) * (year_end - current).days
    return weighted_sum / total_days


@dataclass
class InvestorResult:
    name: str
    total_deposited: float    # net cost basis still in fund (gross deposits - withdrawn basis)
    current_equity: float
    dollar_pnl: float
    pct_pnl: float
    portfolio_share: float
    total_withdrawn: float = 0.0   # cumulative cash proceeds already paid out


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
        current_equity = _net_units(inv) * spy_price

        gross_deposited = sum(d.amount for d in inv.deposits)
        withdrawn_basis = sum(w.cost_basis for w in inv.withdrawals)
        withdrawn_proceeds = sum(w.proceeds for w in inv.withdrawals)
        net_cost_basis = gross_deposited - withdrawn_basis

        dollar_pnl = current_equity - net_cost_basis
        pct_pnl = (dollar_pnl / net_cost_basis * 100) if net_cost_basis else 0.0
        results.append(
            InvestorResult(
                name=inv.name,
                total_deposited=net_cost_basis,
                current_equity=current_equity,
                dollar_pnl=dollar_pnl,
                pct_pnl=pct_pnl,
                portfolio_share=0.0,
                total_withdrawn=withdrawn_proceeds,
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


def compute_withdrawal_lots(
    investor: Investor,
    withdraw_amount: float,
    current_spy: float,
) -> tuple[list[dict], float]:
    """FIFO-match a dollar withdrawal against the investor's deposit lots.

    Returns (lots, units_redeemed).
    Each lot: cost, proceeds, gain, units, short_term, entry_date, entry_spy, holding_days.

    Prior withdrawals are respected — their consumed units are skipped FIFO-style
    so each lot is only redeemed once across the investor's full withdrawal history.
    """
    today = datetime.now(_ET).date()
    available_equity = _net_units(investor) * current_spy

    if withdraw_amount > available_equity + 0.005:
        raise ValueError(
            f"Withdrawal ${withdraw_amount:,.2f} exceeds available equity "
            f"${available_equity:,.2f}"
        )

    units_to_redeem = withdraw_amount / current_spy
    lots: list[dict] = []
    remaining = units_to_redeem

    # Units already consumed by prior withdrawals, applied FIFO across deposits
    prior_consumed = sum(w.units for w in investor.withdrawals)

    for d in investor.deposits:
        if remaining <= 1e-9:
            break
        if not d.entry_spy:
            continue

        deposit_total_units = d.amount / d.entry_spy
        already_consumed = min(prior_consumed, deposit_total_units)
        prior_consumed = max(0.0, prior_consumed - already_consumed)
        available_from_lot = deposit_total_units - already_consumed

        if available_from_lot <= 1e-9:
            continue

        consume = min(remaining, available_from_lot)
        lot_cost = consume * d.entry_spy
        lot_proceeds = consume * current_spy

        try:
            entry_date = _date.fromisoformat(d.date)
        except (ValueError, TypeError):
            entry_date = today
        holding_days = (today - entry_date).days

        lots.append({
            "cost":         lot_cost,
            "proceeds":     lot_proceeds,
            "gain":         lot_proceeds - lot_cost,
            "units":        consume,
            "short_term":   holding_days < 365,
            "entry_date":   d.date,
            "entry_spy":    d.entry_spy,
            "holding_days": holding_days,
        })
        remaining -= consume

    return lots, units_to_redeem


def format_withdrawal_message(
    investor: Investor,
    lots: list[dict],
    units_redeemed: float,
    current_spy: float,
    withdraw_amount: float,
) -> str:
    """Build the Discord notification for a completed withdrawal.

    Called BEFORE the withdrawal is appended to investor.withdrawals so that
    remaining-position math (which calls _net_units) is based on pre-withdrawal state.
    """
    today = datetime.now(_ET)
    today_str = today.strftime(f"%B {today.day}, {today.year}")

    total_cost = sum(l["cost"] for l in lots)
    total_gain = withdraw_amount - total_cost

    st_gain  = sum(l["gain"] for l in lots if l["short_term"]  and l["gain"] > 0)
    st_loss  = sum(l["gain"] for l in lots if l["short_term"]  and l["gain"] < 0)
    lt_gain  = sum(l["gain"] for l in lots if not l["short_term"] and l["gain"] > 0)
    lt_loss  = sum(l["gain"] for l in lots if not l["short_term"] and l["gain"] < 0)
    st_net   = st_gain + st_loss
    lt_net   = lt_gain + lt_loss
    has_lt   = lt_gain != 0 or lt_loss != 0

    # Conservative top-bracket federal estimate (37% ST, 20% LT)
    st_tax_est    = max(st_net, 0.0) * 0.37
    lt_tax_est    = max(lt_net, 0.0) * 0.20
    total_tax_est = st_tax_est + lt_tax_est
    after_tax     = withdraw_amount - total_tax_est

    # Remaining position (pre-withdrawal state minus units_redeemed)
    remaining_units   = _net_units(investor) - units_redeemed
    remaining_equity  = remaining_units * current_spy
    gross_deposited   = sum(d.amount for d in investor.deposits)
    prior_basis       = sum(w.cost_basis for w in investor.withdrawals)
    remaining_basis   = gross_deposited - prior_basis - total_cost
    unrealized_pnl    = remaining_equity - remaining_basis
    unrealized_pct    = (unrealized_pnl / remaining_basis * 100) if remaining_basis else 0.0

    def _s(v: float) -> str:
        return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"

    gain_emoji = "🟢" if total_gain >= 0 else "🔴"

    lines = [
        f"💸 **Investor Withdrawal — {investor.name}**",
        f"SPY @ ${current_spy:,.2f}  ·  {today_str}",
        "",
        f"**Proceeds: ${withdraw_amount:,.2f}**  ·  Cost Basis: ${total_cost:,.2f}",
        f"**Realized P&L: {_s(total_gain)} {gain_emoji}**",
        "",
    ]

    if len(lots) > 1:
        lines.append("**FIFO Tax Lots:**")
        for i, lot in enumerate(lots, 1):
            term = "SHORT-TERM" if lot["short_term"] else "LONG-TERM"
            lines.append(
                f"> Lot {i}: entered {lot['entry_date']} @ ${lot['entry_spy']:.2f}"
                f" ({lot['holding_days']}d — {term})"
            )
            lines.append(
                f">  Cost ${lot['cost']:,.2f} → ${lot['proceeds']:,.2f}"
                f"  ·  **{_s(lot['gain'])}**"
            )
        lines.append("")
    else:
        lot = lots[0]
        term = "SHORT-TERM" if lot["short_term"] else "LONG-TERM"
        lines.append(
            f"Entered {lot['entry_date']} @ ${lot['entry_spy']:.2f}"
            f" · {lot['holding_days']} days held · **{term}**"
        )
        lines.append("")

    lines.append("**⚖️ Estimated Federal Tax:**")
    if st_net != 0 or not has_lt:
        lines.append(f"> Short-term net: {_s(st_net)}  →  est. tax ${st_tax_est:,.2f} (37%)")
    if has_lt:
        lines.append(f"> Long-term net:  {_s(lt_net)}  →  est. tax ${lt_tax_est:,.2f} (20%)")
    lines.append(f"> **Total est. tax: ${total_tax_est:,.2f}**")
    lines.append(f"> **After-tax take-home: ${after_tax:,.2f}**")
    lines.append("> ⚠️ Federal only · excludes state taxes · consult a tax professional")
    lines.append("")

    lines.append(f"**{investor.name} — Remaining Position:**")
    if remaining_basis > 0:
        lines.append(f"> Equity: ${remaining_equity:,.2f}")
        lines.append(f"> Cost Basis: ${remaining_basis:,.2f}")
        lines.append(f"> Unrealized P&L: {_s(unrealized_pnl)} ({unrealized_pct:+.2f}%)")
    else:
        lines.append("> Fully withdrawn — no remaining position")

    return "\n".join(lines)


def format_discord_message(breakdown: InvestorBreakdown, date_str: str) -> str:
    lines = [
        f"📊 **Investor Breakdown — {date_str}**",
        f"SPY: ${breakdown.spy_price:,.2f}",
        "",
    ]
    for r in breakdown.investors:
        pnl_str = (
            f"+${r.dollar_pnl:,.2f} (+{r.pct_pnl:.2f}%)"
            if r.dollar_pnl >= 0
            else f"-${abs(r.dollar_pnl):,.2f} ({r.pct_pnl:.2f}%)"
        )
        lines += [
            f"**{r.name}**",
            f"> Cost Basis (in fund): ${r.total_deposited:,.2f}",
            f"> Current Equity: ${r.current_equity:,.2f}",
            f"> P&L: {pnl_str}",
            f"> Portfolio Share: {r.portfolio_share:.1f}%",
        ]
        if r.total_withdrawn > 0:
            lines.append(f"> Total Withdrawn: ${r.total_withdrawn:,.2f}")
        lines.append("")
    pnl_str = (
        f"+${breakdown.overall_dollar_pnl:,.2f} (+{breakdown.overall_pct_pnl:.2f}%)"
        if breakdown.overall_dollar_pnl >= 0
        else f"-${abs(breakdown.overall_dollar_pnl):,.2f} ({breakdown.overall_pct_pnl:.2f}%)"
    )
    lines += [
        "─" * 25,
        f"**Total Portfolio: ${breakdown.total_portfolio:,.2f}**",
        f"**Total Cost Basis: ${breakdown.total_deposited:,.2f}**",
        f"**Overall P&L: {pnl_str}**",
    ]
    return "\n".join(lines)
