"""
Diagnostic script: dump the raw Alpaca portfolio history used for the
"Since Inception" Discord report, and show exactly what generate_equity_chart()
would plot — to debug the "flat line / missing days" issue.

Run with real Alpaca credentials in the environment:
    ALPACA_API_KEY=... ALPACA_SECRET_KEY=... ALPACA_BASE_URL=... \
        py scripts/check_inception_history.py
"""
import sys
from datetime import datetime
from pathlib import Path

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pnl import FUND_INCEPTION_DATE, _first_nonzero_idx
from app.trading.alpaca_client import get_account, get_portfolio_history

ET = pytz.timezone("America/New_York")


def main() -> None:
    start_dt = ET.localize(datetime.combine(FUND_INCEPTION_DATE, datetime.min.time()))
    now = datetime.now(ET)
    print(f"Requesting portfolio history: timeframe=1D, start={start_dt.isoformat()}")

    history = get_portfolio_history(timeframe="1D", start=start_dt)

    raw_equity = list(history.equity)
    raw_timestamps = list(history.timestamp)
    print(f"\nRaw arrays from Alpaca: {len(raw_equity)} equity points, "
          f"{len(raw_timestamps)} timestamps")

    print("\n--- Raw (date, equity) pairs ---")
    for ts, eq in zip(raw_timestamps, raw_equity):
        d = datetime.fromtimestamp(ts, tz=ET).date()
        flag = "  <-- None/zero" if not eq else ""
        print(f"{d}  {eq!r}{flag}")

    # --- Test fix hypothesis: also pass an explicit `period` covering the full span ---
    days_span = (now.date() - FUND_INCEPTION_DATE).days + 1
    period_str = f"{days_span}D"
    print(f"\n\n=== Retry with explicit period={period_str!r} alongside start= ===")
    history2 = get_portfolio_history(timeframe="1D", start=start_dt, period=period_str)
    raw_equity2 = list(history2.equity)
    raw_timestamps2 = list(history2.timestamp)
    print(f"\nRaw arrays from Alpaca: {len(raw_equity2)} equity points, "
          f"{len(raw_timestamps2)} timestamps")

    print("\n--- Raw (date, equity) pairs (with period=) ---")
    for ts, eq in zip(raw_timestamps2, raw_equity2):
        d = datetime.fromtimestamp(ts, tz=ET).date()
        flag = "  <-- None/zero" if not eq else ""
        print(f"{d}  {eq!r}{flag}")

    # --- Check send_alltime_report's period="all" for the same capping issue ---
    print("\n\n=== send_alltime_report's period='all' (no start=) ===")
    history3 = get_portfolio_history(timeframe="1D", period="all")
    raw_equity3 = list(history3.equity)
    raw_timestamps3 = list(history3.timestamp)
    print(f"\nRaw arrays from Alpaca: {len(raw_equity3)} equity points, "
          f"{len(raw_timestamps3)} timestamps")

    print("\n--- Raw (date, equity) pairs (period='all') ---")
    for ts, eq in zip(raw_timestamps3, raw_equity3):
        d = datetime.fromtimestamp(ts, tz=ET).date()
        flag = "  <-- None/zero" if not eq else ""
        print(f"{d}  {eq!r}{flag}")

    # Reproduce _send_since_date_report's slicing/extension logic
    start_idx = _first_nonzero_idx(raw_equity)
    equity = raw_equity[start_idx:]
    timestamps = raw_timestamps[start_idx:]

    last_date = datetime.fromtimestamp(timestamps[-1], tz=ET).date()
    print(f"\nAfter _first_nonzero_idx slice: {len(equity)} points, "
          f"last historical date = {last_date}, today = {now.date()}")

    if last_date < now.date():
        account = get_account()
        equity = equity + [float(account.equity)]
        timestamps = timestamps + [int(now.timestamp())]
        print(f"Appended live 'today' point: equity={account.equity}, date={now.date()}")

    # Reproduce generate_equity_chart's None-filter
    dates = [datetime.fromtimestamp(ts, tz=ET).date() for ts in timestamps]
    pairs = [(d, eq) for d, eq in zip(dates, equity) if eq is not None]
    chart_dates, chart_equity = (list(t) for t in zip(*pairs)) if pairs else ([], [])

    print(f"\n--- What generate_equity_chart() actually plots ({len(chart_dates)} points) ---")
    prev = None
    for d, eq in zip(chart_dates, chart_equity):
        gap = ""
        if prev is not None:
            delta = (d - prev).days
            if delta > 3:
                gap = f"  <-- GAP of {delta} days since previous point"
        print(f"{d}  {eq:.2f}{gap}")
        prev = d


if __name__ == "__main__":
    sys.exit(main())
