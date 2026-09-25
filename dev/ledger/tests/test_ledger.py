"""The ledger end to end: the drift gate, the idempotent load, the report."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from ledger.cli import DRIFT_EXIT, cli
from ledger.ingest import Drift, ingest
from ledger.report import month_bounds, monthly
from ledger.schema import drift, load_schema

HERE = Path(__file__).resolve().parents[1]
GOOD = HERE / "fixtures" / "expenses-2026-08.csv"
DRIFTED = HERE / "fixtures" / "expenses-drifted.csv"
HEADER = ["date", "category", "description", "amount_eur"]


def test_the_schema_names_four_typed_columns() -> None:
    schema = load_schema()
    assert schema.names == tuple(HEADER)
    assert [c.type for c in schema.columns] == ["date", "text", "text", "decimal"]


def test_a_matching_file_has_no_drift() -> None:
    assert drift(HEADER, [["2026-08-01", "rent", "August rent", "950.00"]]) == []


def test_a_renamed_column_is_named_both_ways() -> None:
    found = drift(["date", "kind", "description", "amount_eur"], [])
    assert found == ["missing column category", "unexpected column kind"]


def test_a_reordered_header_is_drift() -> None:
    found = drift(["category", "date", "description", "amount_eur"], [])
    assert len(found) == 1 and "out of order" in found[0]


def test_a_string_in_amount_eur_is_named_with_its_line() -> None:
    found = drift(HEADER, [["2026-08-02", "groceries", "shop", "sixty-four"]])
    assert found == ["column amount_eur: 'sixty-four' on line 2 is not a decimal"]


def test_an_impossible_date_and_a_short_row_are_named() -> None:
    found = drift(HEADER, [["2026-02-30", "a", "b", "1"], ["2026-08-01", "a"]])
    assert found == [
        "column date: '2026-02-30' on line 2 is not a date",
        "line 3 has 2 fields, expected 4",
    ]


def test_ingest_stores_every_row_once(tmp_path: Path) -> None:
    db = tmp_path / "ledger.duckdb"
    first = ingest(GOOD, db)
    assert (first.rows, first.already) == (12, False)
    again = ingest(GOOD, db)
    assert (again.rows, again.already, again.sha256) == (12, True, first.sha256)
    assert monthly(db, "2026-08").total == Decimal("1486.09")


def test_the_same_rows_under_another_name_are_the_same_load(tmp_path: Path) -> None:
    copy = tmp_path / "renamed.csv"
    shutil.copyfile(GOOD, copy)
    db = tmp_path / "ledger.duckdb"
    ingest(GOOD, db)
    assert ingest(copy, db).already is True


def test_a_drifted_file_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "ledger.duckdb"
    ingest(GOOD, db)
    with pytest.raises(Drift) as caught:
        ingest(DRIFTED, db)
    assert "missing column category" in caught.value.differences
    assert monthly(db, "2026-08").total == Decimal("1486.09")


def test_the_report_totals_each_category_largest_first(tmp_path: Path) -> None:
    db = tmp_path / "ledger.duckdb"
    ingest(GOOD, db)
    report = monthly(db, "2026-08")
    assert report.categories[0] == ("rent", Decimal("950.00"))
    assert dict(report.categories)["groceries"] == Decimal("203.20")
    assert sum(amount for _, amount in report.categories) == report.total


def test_an_empty_month_totals_zero(tmp_path: Path) -> None:
    db = tmp_path / "ledger.duckdb"
    ingest(GOOD, db)
    report = monthly(db, "2026-09")
    assert (report.total, report.categories) == (Decimal("0.00"), ())


@pytest.mark.parametrize("month", ["2026-8", "2026-13", "August", "2026-08-01"])
def test_a_month_not_in_yyyy_mm_is_refused(month: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        month_bounds(month)


def test_december_ends_at_the_new_year() -> None:
    start, end = month_bounds("2026-12")
    assert (start.isoformat(), end.isoformat()) == ("2026-12-01", "2027-01-01")


def test_the_cli_ingests_reports_and_refuses_drift_with_exit_3(tmp_path: Path) -> None:
    db = str(tmp_path / "ledger.duckdb")
    runner = CliRunner()
    done = runner.invoke(cli, ["ingest", str(GOOD), "--db", db])
    assert done.exit_code == 0, done.output
    assert "loaded 12 rows" in done.output
    report = runner.invoke(cli, ["report", "--db", db, "--month", "2026-08"])
    assert report.exit_code == 0, report.output
    assert "total 1486.09 EUR" in report.output
    refused = runner.invoke(cli, ["ingest", str(DRIFTED), "--db", db])
    assert refused.exit_code == DRIFT_EXIT == 3
    assert "schema drift: missing column category" in refused.output
    bad_month = runner.invoke(cli, ["report", "--db", db, "--month", "2026-8"])
    assert bad_month.exit_code == 2
