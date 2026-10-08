import gc

from matplotlib.figure import Figure

from app.financials_chart import generate_financials_chart

_DATA = {
    "ticker": "TEST",
    "quarters": ["Q1'25", "Q2'25", "Q3'25", "Q4'25", "Q1'26"],
    "revenue": [9e9, 1e10, 1.1e10, 1.2e10, 1.3e10],
    "gross_profit": [5e9, 6e9, 6e9, 7e9, 8e9],
    "net_income": [1e9, -1e9, 2e9, 2e9, 3e9],
    "net_margin": [11.1, -10.0, 18.2, 16.7, 23.1],
}


def _live_figures() -> int:
    return sum(1 for o in gc.get_objects() if isinstance(o, Figure))


def test_chart_figures_do_not_outlive_the_render():
    """Each render must free its own Figure before returning.

    The monthly rebalance renders one chart per ticker back to back. On
    2026-10-01 nine Figures (and their ~7 MB pixel buffers) were all still
    alive at job end and RSS rose 163 MB, never to come back down.
    """
    gc.collect()
    before = _live_figures()
    for _ in range(3):
        png = generate_financials_chart(_DATA)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _live_figures() == before
