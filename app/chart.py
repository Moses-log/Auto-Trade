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
_BG           = "#07071a"
_PANEL        = "#0d0d2b"
_PORT_COLOR   = "#00ff88"   # neon green
_SPY_COLOR    = "#ff2d78"   # neon magenta
_ZERO_COLOR   = "#2a2a6a"
_GRID_COLOR   = "#111133"
_TEXT_COLOR   = "#a0a0d0"
_TITLE_COLOR  = "#00e5ff"   # neon cyan


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
    ax.set_title(title, fontsize=13, fontweight="bold",
                 color=_TITLE_COLOR, pad=12)
    ax.set_ylabel("Return (%)", color=_TEXT_COLOR, fontsize=10)

    # Axis formatting
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:+.1f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())

    # Tick colors
    ax.tick_params(axis="x", colors=_TEXT_COLOR, rotation=30)
    ax.tick_params(axis="y", colors=_TEXT_COLOR)
    plt.setp(ax.get_xticklabels(), ha="right", fontsize=9)

    # Spine styling
    for spine in ax.spines.values():
        spine.set_edgecolor(_ZERO_COLOR)
        spine.set_linewidth(0.8)

    # Grid
    ax.grid(True, color=_GRID_COLOR, linewidth=0.8, alpha=1.0)
    ax.set_axisbelow(True)

    # Legend
    legend = ax.legend(
        loc="upper left",
        facecolor=_BG,
        edgecolor=_ZERO_COLOR,
        labelcolor=_TEXT_COLOR,
        fontsize=10,
        framealpha=0.85,
    )
    for line in legend.get_lines():
        line.set_linewidth(2.5)

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
