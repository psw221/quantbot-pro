from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from tax.report_export import build_tax_export_payload, export_tax_report


class FakeTaxCalculator:
    def calculate_yearly_summary(self, year: int, market: str | None = None) -> dict[str, object]:
        return {
            "year": year,
            "market": market,
            "sell_trade_count": 1,
            "total_quantity": 3,
            "realized_gain_loss_krw": 120000.0,
            "taxable_gain_krw": 150000.0,
            "total_fees_krw": 3000.0,
            "total_taxes_krw": 1000.0,
            "by_market": {
                market or "US": {
                    "sell_trade_count": 1,
                    "total_quantity": 3,
                    "realized_gain_loss_krw": 120000.0,
                    "taxable_gain_krw": 150000.0,
                    "total_fees_krw": 3000.0,
                    "total_taxes_krw": 1000.0,
                }
            },
        }

    def build_trade_report(self, year: int, market: str | None = None) -> list[dict[str, object]]:
        return [
            {
                "trade_id": 11,
                "ticker": "AAPL",
                "market": market or "US",
                "strategy": "dual_momentum",
                "sell_date": datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc),
                "quantity": 3,
                "currency": "USD",
                "sell_price": 180.0,
                "gross_proceeds_local": 540.0,
                "cost_basis_local": 400.0,
                "realized_gain_loss_local": 135.0,
                "gross_proceeds_krw": 756000.0,
                "cost_basis_krw": 560000.0,
                "fee_krw": 3000.0,
                "tax_krw": 1000.0,
                "realized_gain_loss_krw": 192000.0,
                "taxable_gain_krw": 196000.0,
                "buy_fx_rate": 1300.0,
                "sell_fx_rate": 1400.0,
                "fx_rate_source": "test",
                "source": "tax_event",
            },
            {
                "trade_id": 12,
                "ticker": "MSFT",
                "market": market or "US",
                "strategy": "trend_following",
                "sell_date": datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
                "quantity": 2,
                "currency": "USD",
                "sell_price": 300.0,
                "gross_proceeds_local": 600.0,
                "cost_basis_local": 520.0,
                "realized_gain_loss_local": 80.0,
                "gross_proceeds_krw": 840000.0,
                "cost_basis_krw": 728000.0,
                "fee_krw": 2000.0,
                "tax_krw": 500.0,
                "realized_gain_loss_krw": 110000.0,
                "taxable_gain_krw": 112000.0,
                "buy_fx_rate": 1310.0,
                "sell_fx_rate": 1400.0,
                "fx_rate_source": "test",
                "source": "tax_event",
            },
        ]


def test_build_tax_export_payload_uses_tax_calculator_contract() -> None:
    payload = build_tax_export_payload(FakeTaxCalculator(), year=2026, market="us")

    assert payload["yearly_summary"]["year"] == 2026
    assert payload["yearly_summary"]["market"] == "US"
    assert payload["trade_report_rows"][0]["market"] == "US"


def test_export_tax_report_writes_json_bundle(tmp_path: Path) -> None:
    result = export_tax_report(
        year=2026,
        market="US",
        output_format="json",
        output_dir=tmp_path,
        calculator=FakeTaxCalculator(),
    )

    assert len(result.output_paths) == 1
    payload = json.loads(result.output_paths[0].read_text(encoding="utf-8"))
    assert payload["yearly_summary"]["market"] == "US"
    assert payload["trade_report_rows"][0]["sell_date"] == "2026-04-21T14:00:00+00:00"


def test_export_tax_report_writes_summary_and_trades_csv(tmp_path: Path) -> None:
    result = export_tax_report(
        year=2026,
        output_format="csv",
        output_dir=tmp_path,
        calculator=FakeTaxCalculator(),
    )

    assert len(result.output_paths) == 2
    summary_path, trades_path = result.output_paths
    with summary_path.open("r", encoding="utf-8", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))
    with trades_path.open("r", encoding="utf-8", newline="") as handle:
        trade_rows = list(csv.DictReader(handle))

    assert summary_rows[0]["scope"] == "total"
    assert summary_rows[0]["report_scope"] == "yearly"
    assert summary_rows[0]["market"] == "all"
    assert trade_rows[0]["ticker"] == "AAPL"
    assert trade_rows[0]["sell_date"] == "2026-04-21T14:00:00+00:00"


def test_build_tax_export_payload_filters_month_and_builds_period_summary() -> None:
    payload = build_tax_export_payload(FakeTaxCalculator(), year=2026, month=4, market="US")

    assert payload["report_scope"] == "monthly"
    assert payload["period_summary"] is not None
    assert payload["period_summary"]["month"] == 4
    assert payload["period_summary"]["sell_trade_count"] == 1
    assert payload["period_summary"]["total_quantity"] == 3
    assert payload["period_summary"]["realized_gain_loss_krw"] == 192000.0
    assert len(payload["trade_report_rows"]) == 1
    assert payload["trade_report_rows"][0]["trade_id"] == 11


def test_export_tax_report_writes_monthly_json_bundle(tmp_path: Path) -> None:
    result = export_tax_report(
        year=2026,
        month=4,
        market="US",
        output_format="json",
        output_dir=tmp_path,
        calculator=FakeTaxCalculator(),
    )

    assert result.report_scope == "monthly"
    assert result.month == 4
    payload = json.loads(result.output_paths[0].read_text(encoding="utf-8"))
    assert payload["report_scope"] == "monthly"
    assert payload["period_summary"]["month"] == 4
    assert payload["period_summary"]["sell_trade_count"] == 1
    assert payload["trade_report_rows"][0]["trade_id"] == 11


def test_export_tax_report_writes_monthly_summary_csv(tmp_path: Path) -> None:
    result = export_tax_report(
        year=2026,
        month=4,
        output_format="csv",
        output_dir=tmp_path,
        calculator=FakeTaxCalculator(),
    )

    summary_path, trades_path = result.output_paths
    with summary_path.open("r", encoding="utf-8", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))
    with trades_path.open("r", encoding="utf-8", newline="") as handle:
        trade_rows = list(csv.DictReader(handle))

    assert summary_rows[0]["report_scope"] == "monthly"
    assert summary_rows[0]["month"] == "4"
    assert summary_rows[0]["period_start"] == "2026-04-01"
    assert summary_rows[0]["period_end"] == "2026-04-30"
    assert summary_rows[0]["sell_trade_count"] == "1"
    assert trade_rows[0]["trade_id"] == "11"
