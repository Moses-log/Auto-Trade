from __future__ import annotations

import io
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — required for server use
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz

ET = pytz.timezone("America/New_York")

# Cyberpunk palette
_RH_LINE_COLOR = "#39ff14"   # neon bright green (RH P&L chart)

_BG           = "#07071a"
_PANEL        = "#0d0d2b"
_PORT_COLOR   = "#00ff88"   # neon green
_SPY_COLOR    = "#ff2d78"   # neon magenta
_ZERO_COLOR   = "#3a3a8a"
_GRID_COLOR   = "#1a1a40"
_TEXT_COLOR   = "#d0d0f0"   # bright lavender — readable on dark bg
_TITLE_COLOR  = "#ffe600"   # neon yellow


def _glow(ax, x, y, color, lw: float = 2.0, label: str | None = None, linestyle: str = "-") -> None:
    """Plot a line with a layered neon glow effect."""
    ax.plot(x, y, color=color, linewidth=lw * 6,   alpha=0.04, zorder=2, linestyle=linestyle)
    ax.plot(x, y, color=color, linewidth=lw * 3.5, alpha=0.10, zorder=3, linestyle=linestyle)
    ax.plot(x, y, color=color, linewidth=lw * 1.8, alpha=0.25, zorder=4, linestyle=linestyle)
    ax.plot(x, y, color=color, linewidth=lw,       alpha=1.00, zorder=5, linestyle=linestyle, label=label)


def generate_equity_chart(
    equity: list,
    timestamps: list,
    spy_df,
    title: str,
) -> bytes:
    """Generate a portfolio vs SPY % return chart as PNG bytes.

    Both series are normalized to % return from the start of the period
    so they share a 0% baseline regardless of dollar amounts.

    Args:
        equity:     Portfolio equity values from Alpaca history.
        timestamps: Corresponding Unix timestamps from Alpaca history.
        spy_df:     yfinance DataFrame with a "Close" column, or None.
        title:      Chart title string.

    Returns:
        PNG image as bytes.
    """
    dates = [datetime.fromtimestamp(ts, tz=ET).date() for ts in timestamps]

    # Drop any mid-series None values Alpaca returns for market holidays
    pairs = [(d, eq) for d, eq in zip(dates, equity) if eq is not None]
    if pairs:
        dates, equity = map(list, zip(*pairs))

    open_eq = equity[0] if equity and equity[0] else 1.0
    port_pct = [(eq - open_eq) / open_eq * 100 for eq in equity]

    spy_pct: list = []
    spy_dates: list = []
    if spy_df is not None and not spy_df.empty and "Close" in spy_df.columns:
        spy_open = float(spy_df["Close"].iloc[0])
        if spy_open:
            spy_pct = [(float(p) - spy_open) / spy_open * 100 for p in spy_df["Close"]]
            spy_dates = [
                d.date() if hasattr(d, "date") else d for d in spy_df.index
            ]

    fig, ax = plt.subplots(figsize=(10, 5))

    # Dark cyberpunk background
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_PANEL)

    # Subtle zero baseline with glow
    ax.axhline(0, color=_ZERO_COLOR, linewidth=1.5, alpha=0.8, zorder=1)
    ax.axhline(0, color=_ZERO_COLOR, linewidth=4,   alpha=0.15, zorder=1)

    # Portfolio line
    final_port = port_pct[-1] if port_pct else 0.0
    port_sign = "+" if final_port >= 0 else ""
    _glow(ax, dates, port_pct, _PORT_COLOR, lw=2,
          label=f"Portfolio  {port_sign}{final_port:.2f}%")

    # Fill under portfolio line
    ax.fill_between(dates, port_pct, 0,
                    where=[p >= 0 for p in port_pct],
                    color=_PORT_COLOR, alpha=0.06, zorder=1)
    ax.fill_between(dates, port_pct, 0,
                    where=[p < 0 for p in port_pct],
                    color=_PORT_COLOR, alpha=0.06, zorder=1)

    # SPY line
    if spy_pct and spy_dates:
        final_spy = spy_pct[-1]
        spy_sign = "+" if final_spy >= 0 else ""
        _glow(ax, spy_dates, spy_pct, _SPY_COLOR, lw=1.5, linestyle="--",
              label=f"S&P 500    {spy_sign}{final_spy:.2f}%")

    # Title and labels
    ax.set_title(title, fontsize=14, fontweight="bold",
                 color=_TITLE_COLOR, pad=14)
    ax.set_ylabel("Return (%)", color=_TEXT_COLOR, fontsize=11, labelpad=8)

    # Axis formatting
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:+.1f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))

    # Tick colors and sizes
    ax.tick_params(axis="x", colors=_TEXT_COLOR, rotation=30, labelsize=10)
    ax.tick_params(axis="y", colors=_TEXT_COLOR, labelsize=10)
    plt.setp(ax.get_xticklabels(), ha="right")

    # Spine styling
    for spine in ax.spines.values():
        spine.set_edgecolor(_ZERO_COLOR)
        spine.set_linewidth(1.2)

    # Grid — bright enough to orient, dark enough not to compete with lines
    ax.grid(True, color=_GRID_COLOR, linewidth=0.8, alpha=1.0)
    ax.set_axisbelow(True)

    # Legend
    legend = ax.legend(
        loc="upper left",
        facecolor="#0a0a22",
        edgecolor=_ZERO_COLOR,
        labelcolor=_TEXT_COLOR,
        fontsize=11,
        framealpha=0.90,
    )
    for line in legend.get_lines():
        line.set_linewidth(2.5)

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_rh_pnl_chart(trades: list, title: str) -> bytes:
    """Generate a cumulative realized P&L chart for RH trades (neon bright green theme).

    trades: list of dicts with 'ts' (ISO datetime str) and 'dollar_pnl' (float).
    Returns PNG bytes, or empty bytes if fewer than 2 valid trade points.
    """
    from datetime import timezone as _tz

    points = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            points.append((ts.astimezone(ET), float(t.get("dollar_pnl", 0.0))))
        except Exception:
            pass

    if len(points) < 2:
        return b""

    points.sort(key=lambda x: x[0])

    x_dates = [p[0] for p in points]
    cumulative: list = []
    running = 0.0
    for _, pnl in points:
        running += pnl
        cumulative.append(running)

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_PANEL)

    ax.axhline(0, color=_ZERO_COLOR, linewidth=1.5, alpha=0.8, zorder=1)
    ax.axhline(0, color=_ZERO_COLOR, linewidth=4,   alpha=0.15, zorder=1)

    final = cumulative[-1]
    sign = "+" if final >= 0 else ""
    _glow(ax, x_dates, cumulative, _RH_LINE_COLOR, lw=2,
          label=f"Realized P&L  {sign}${final:,.2f}")

    ax.fill_between(x_dates, cumulative, 0,
                    where=[c >= 0 for c in cumulative],
                    color=_RH_LINE_COLOR, alpha=0.08, zorder=1)
    ax.fill_between(x_dates, cumulative, 0,
                    where=[c < 0 for c in cumulative],
                    color=_RH_LINE_COLOR, alpha=0.08, zorder=1)

    ax.set_title(title, fontsize=14, fontweight="bold",
                 color=_TITLE_COLOR, pad=14)
    ax.set_ylabel("Realized P&L ($)", color=_TEXT_COLOR, fontsize=11, labelpad=8)

    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"${y:,.0f}"))
    locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.AutoDateFormatter(locator))

    ax.tick_params(axis="x", colors=_TEXT_COLOR, rotation=30, labelsize=10)
    ax.tick_params(axis="y", colors=_TEXT_COLOR, labelsize=10)
    plt.setp(ax.get_xticklabels(), ha="right")

    for spine in ax.spines.values():
        spine.set_edgecolor(_ZERO_COLOR)
        spine.set_linewidth(1.2)

    ax.grid(True, color=_GRID_COLOR, linewidth=0.8, alpha=1.0)
    ax.set_axisbelow(True)

    legend = ax.legend(
        loc="upper left",
        facecolor="#0a0a22",
        edgecolor=_ZERO_COLOR,
        labelcolor=_TEXT_COLOR,
        fontsize=11,
        framealpha=0.90,
    )
    for line in legend.get_lines():
        line.set_linewidth(2.5)

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
