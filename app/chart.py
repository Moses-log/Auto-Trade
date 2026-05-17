from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — required for server use
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pytz

ET = pytz.timezone("America/New_York")


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

    ax.axhline(0, color="grey", linestyle="--", linewidth=0.8, alpha=0.5)

    final_port = port_pct[-1] if port_pct else 0.0
    port_sign = "+" if final_port >= 0 else ""
    ax.plot(
        dates, port_pct,
        color="#2ecc71", linewidth=2,
        label=f"Portfolio {port_sign}{final_port:.2f}%",
    )

    if spy_pct and spy_dates:
        final_spy = spy_pct[-1]
        spy_sign = "+" if final_spy >= 0 else ""
        ax.plot(
            spy_dates, spy_pct,
            color="#e67e22", linestyle="--", linewidth=1.5,
            label=f"S&P 500 {spy_sign}{final_spy:.2f}%",
        )

    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_ylabel("Return (%)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:+.1f}%"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=30, ha="right")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
