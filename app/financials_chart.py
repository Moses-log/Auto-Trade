"""
financials_chart.py — Cyberpunk quarterly financials bar chart for Claude trades.

Fetches the last 5 quarters of income statement data from yfinance and renders
a grouped bar + line chart styled like Robinhood's Financials section:
  • Revenue, Gross Profit, Net Income → grouped bars
  • Net Margin → line overlay on secondary y-axis
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
import yfinance as yf

log = logging.getLogger(__name__)

# ── Cyberpunk palette ─────────────────────────────────────────────────────────
_BG          = "#030303"
_PANEL       = "#0a0a22"
_TEXT_COLOR  = "#d0d0f0"
_TITLE_COLOR = "#ffe600"
_GRID_COLOR  = "#111122"
_ZERO_LINE   = "#2a2a3a"

_COLOR_REVENUE = "#ffe600"   # neon yellow
_COLOR_GP      = "#39ff14"   # neon green
_COLOR_NI_POS  = "#00f7ff"   # neon cyan — profitable quarter
_COLOR_NI_NEG  = "#ff2d78"   # neon pink  — loss quarter
_COLOR_MARGIN  = "#ff6600"   # neon orange line


def _quarter_label(ts) -> str:
    """Convert a pandas Timestamp to e.g. 'Q2'25'."""
    quarter = (ts.month - 1) // 3 + 1
    return f"Q{quarter}'{str(ts.year)[-2:]}"


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return None if f != f else f   # NaN != NaN
    except (TypeError, ValueError):
        return None


def _scale(values: list[Optional[float]]) -> tuple[float, str]:
    """Return (divisor, suffix) based on the largest absolute value."""
    top = max((abs(v) for v in values if v is not None), default=0)
    if top >= 1e9:
        return 1e9, "B"
    return 1e6, "M"


def fetch_quarterly_financials(ticker: str) -> Optional[dict]:
    """
    Fetch the last 5 quarters of income-statement data.
    Returns a dict with keys: ticker, quarters, revenue, gross_profit,
    net_income, net_margin. Returns None if data is unavailable.
    """
    try:
        t = yf.Ticker(ticker)
        stmt = t.quarterly_income_stmt
        if stmt is None or stmt.empty:
            return None

        # Columns are newest-first; keep ≤ 5, reverse to chronological order.
        cols = list(stmt.columns[:5])[::-1]
        if not cols:
            return None

        def _row(*names: str) -> list[Optional[float]]:
            for name in names:
                if name in stmt.index:
                    return [_safe_float(stmt.loc[name, c]) for c in cols]
            return [None] * len(cols)

        revenues  = _row("Total Revenue", "Revenue")
        gps       = _row("Gross Profit")
        net_incs  = _row("Net Income", "Net Income Common Stockholders")

        margins: list[Optional[float]] = []
        for rev, ni in zip(revenues, net_incs):
            if rev and ni and rev != 0:
                margins.append(round(ni / rev * 100, 2))
            else:
                margins.append(None)

        # Need at least revenue data to make a meaningful chart
        if all(v is None for v in revenues):
            return None

        return {
            "ticker":       ticker.upper(),
            "quarters":     [_quarter_label(c) for c in cols],
            "revenue":      revenues,
            "gross_profit": gps,
            "net_income":   net_incs,
            "net_margin":   margins,
        }

    except Exception as exc:
        log.warning("Quarterly financials fetch failed for %s: %s", ticker, exc)
        return None


def generate_financials_chart(data: dict) -> bytes:
    """Render a cyberpunk grouped-bar + margin-line chart. Returns PNG bytes."""
    ticker    = data["ticker"]
    quarters  = data["quarters"]
    revenues  = data["revenue"]
    gps       = data["gross_profit"]
    net_incs  = data["net_income"]
    margins   = data["net_margin"]

    n = len(quarters)
    x = np.arange(n, dtype=float)
    bar_w = 0.22

    # Scale bars to M or B based on revenue magnitude
    divisor, suffix = _scale(revenues)

    def _scaled(vals: list[Optional[float]]) -> list[float]:
        return [(v / divisor if v is not None else 0.0) for v in vals]

    rev_s = _scaled(revenues)
    gp_s  = _scaled(gps)
    ni_s  = _scaled(net_incs)

    fig, ax1 = plt.subplots(figsize=(11, 6))
    fig.patch.set_facecolor(_BG)
    ax1.set_facecolor(_BG)

    # ── Grouped bars ──────────────────────────────────────────────────────────
    ax1.bar(x - bar_w, rev_s, bar_w, color=_COLOR_REVENUE, alpha=0.88, zorder=3, label="Revenue")
    ax1.bar(x,         gp_s,  bar_w, color=_COLOR_GP,      alpha=0.88, zorder=3, label="Gross Profit")

    # Net Income bars coloured per-quarter (positive = cyan, negative = pink)
    for i in range(n):
        color = _COLOR_NI_POS if (net_incs[i] is not None and net_incs[i] >= 0) else _COLOR_NI_NEG
        ax1.bar(x[i] + bar_w, ni_s[i], bar_w, color=color, alpha=0.88, zorder=3)

    # Dummy bar for legend entry
    ax1.bar([], [], bar_w, color=_COLOR_NI_POS, alpha=0.88, label="Net Income")

    # ── Net margin line on secondary axis ─────────────────────────────────────
    ax2 = ax1.twinx()
    ax2.patch.set_visible(False)  # transparent — bars on ax1 show through

    valid_x = [x[i] for i, m in enumerate(margins) if m is not None]
    valid_y = [m for m in margins if m is not None]

    if valid_x:
        ax2.plot(valid_x, valid_y,
                 color=_COLOR_MARGIN, linewidth=2.5, marker="o",
                 markersize=7, zorder=5, solid_capstyle="round")
        for xi, yi in zip(valid_x, valid_y):
            va = "bottom" if yi >= 0 else "top"
            offset = 10 if yi >= 0 else -12
            ax2.annotate(
                f"{yi:.1f}%",
                xy=(xi, yi),
                xytext=(0, offset),
                textcoords="offset points",
                ha="center", fontsize=8.5,
                color=_COLOR_MARGIN, fontweight="bold",
            )

    # ── Axis styling ──────────────────────────────────────────────────────────
    for ax in (ax1, ax2):
        for spine in ax.spines.values():
            spine.set_visible(False)

    ax1.set_xticks(x)
    ax1.set_xticklabels(quarters, color=_TEXT_COLOR, fontsize=11.5, fontweight="bold")
    ax1.tick_params(axis="x", colors=_TEXT_COLOR, bottom=False, length=0)
    ax1.tick_params(axis="y", colors="#888899", labelsize=9, length=0)
    ax2.tick_params(axis="y", colors=_COLOR_MARGIN, labelsize=9, length=0)

    ax1.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"${v:.1f}{suffix}")
    )
    ax2.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: f"{v:.0f}%")
    )

    ax1.grid(axis="y", color=_GRID_COLOR, linewidth=0.9, zorder=0)
    ax1.axhline(0, color=_ZERO_LINE, linewidth=1.2, zorder=2)
    ax1.set_xlim(-0.6, n - 0.4)

    ax1.set_ylabel(f"USD ({suffix})", color="#888899", fontsize=10)
    ax2.set_ylabel("Net Margin", color=_COLOR_MARGIN, fontsize=10)

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_handles = [
        Patch(facecolor=_COLOR_REVENUE, alpha=0.88, label="Revenue"),
        Patch(facecolor=_COLOR_GP,      alpha=0.88, label="Gross Profit"),
        Patch(facecolor=_COLOR_NI_POS,  alpha=0.88, label="Net Income"),
        Line2D([0], [0], color=_COLOR_MARGIN, linewidth=2.5,
               marker="o", markersize=7, label="Net Margin"),
    ]
    legend = ax1.legend(
        handles=legend_handles,
        loc="upper left",
        facecolor=_PANEL,
        edgecolor=_ZERO_LINE,
        labelcolor=_TEXT_COLOR,
        fontsize=9.5,
        framealpha=0.92,
    )

    ax1.set_title(
        f"{ticker}  ·  Quarterly Financials",
        color=_TITLE_COLOR, fontsize=14, fontweight="bold", pad=16,
    )

    fig.tight_layout(pad=1.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
