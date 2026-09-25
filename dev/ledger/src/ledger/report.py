"""The monthly report: the total and the total per category."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ledger.ingest import connect

MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

# The spend per category in one month, largest first; the name breaks a tie so
# the same data always prints the same report.
PER_CATEGORY = """
select category, sum(amount_eur) as amount
from expenses
where date >= ? and date < ?
group by category
order by amount desc, category
"""


@dataclass(frozen=True, slots=True)
class Report:
    month: str
    total: Decimal
    categories: tuple[tuple[str, Decimal], ...]

    def text(self) -> str:
        width = max((len(name) for name, _ in self.categories), default=8)
        lines = [f"month {self.month}", f"total {self.total:.2f} EUR"]
        lines += [
            f"  {name:<{width}}  {amount:>10.2f}" for name, amount in self.categories
        ]
        return "\n".join(lines)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    found = MONTH.match(month)
    if found is None:
        raise ValueError(f"{month!r} is not a month in the form YYYY-MM")
    year, number = int(found.group(1)), int(found.group(2))
    start = dt.date(year, number, 1)
    end = dt.date(year + 1, 1, 1) if number == 12 else dt.date(year, number + 1, 1)
    return start, end


def monthly(db: Path, month: str) -> Report:
    start, end = month_bounds(month)
    con = connect(db)
    try:
        rows = con.execute(PER_CATEGORY, [start, end]).fetchall()
    finally:
        con.close()
    categories = tuple((str(name), Decimal(amount)) for name, amount in rows)
    total = sum((amount for _, amount in categories), Decimal("0.00"))
    return Report(month, total, categories)
