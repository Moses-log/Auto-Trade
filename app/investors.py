from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

INVESTORS_FILE = Path(__file__).parent.parent / "investors.json"


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
        return []
    data = json.loads(path.read_text())
    return [
        Investor(
            name=inv["name"],
            deposits=[Deposit(**d) for d in inv["deposits"]],
        )
        for inv in data["investors"]
    ]


def save_investors(investors: list[Investor], path: Path = INVESTORS_FILE) -> None:
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
    path.write_text(json.dumps(data, indent=2))
