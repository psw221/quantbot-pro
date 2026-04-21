from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from tax.tax_calculator import TaxCalculator


SUMMARY_FIELDS = [
    "scope",
    "market",
    "year",
    "sell_trade_count",
    "total_quantity",
    "realized_gain_loss_krw",
    "taxable_gain_krw",
    "total_fees_krw",
    "total_taxes_krw",
]

TRADE_REPORT_FIELDS = [
    "trade_id",
    "ticker",
    "market",
    "strategy",
    "sell_date",
    "quantity",
    "currency",
    "sell_price",
    "gross_proceeds_local",
    "cost_basis_local",
    "realized_gain_loss_local",
    "gross_proceeds_krw",
    "cost_basis_krw",
    "fee_krw",
    "tax_krw",
    "realized_gain_loss_krw",
    "taxable_gain_krw",
    "buy_fx_rate",
    "sell_fx_rate",
    "fx_rate_source",
    "source",
]


@dataclass(slots=True)
class TaxExportResult:
    format: str
    year: int
    market: str | None
    output_paths: list[Path]
    yearly_summary: dict[str, Any]
    trade_report_rows: list[dict[str, Any]]


def build_tax_export_payload(
    calculator: TaxCalculator,
    *,
    year: int,
    market: str | None = None,
) -> dict[str, Any]:
    normalized_market = None if market is None else market.upper()
    return {
        "yearly_summary": calculator.calculate_yearly_summary(year, market=normalized_market),
        "trade_report_rows": calculator.build_trade_report(year, market=normalized_market),
    }


def export_tax_report(
    *,
    year: int,
    market: str | None = None,
    output_format: str = "json",
    output_dir: str | Path = Path("reports") / "tax",
    output_stem: str | None = None,
    calculator: TaxCalculator | None = None,
) -> TaxExportResult:
    normalized_market = None if market is None else market.upper()
    if normalized_market not in (None, "KR", "US"):
        raise ValueError("market must be KR, US, or omitted")

    export_format = output_format.lower()
    if export_format not in {"json", "csv"}:
        raise ValueError("output_format must be json or csv")

    export_calculator = calculator or TaxCalculator()
    payload = build_tax_export_payload(export_calculator, year=year, market=normalized_market)
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    base_name = output_stem or _default_output_stem(year=year, market=normalized_market)

    if export_format == "json":
        output_path = destination_dir / f"{base_name}.json"
        output_path.write_text(
            json.dumps(_normalize_payload(payload), indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        output_paths = [output_path]
    else:
        summary_path = destination_dir / f"{base_name}_summary.csv"
        trades_path = destination_dir / f"{base_name}_trades.csv"
        _write_csv(summary_path, SUMMARY_FIELDS, _build_summary_rows(payload["yearly_summary"]))
        _write_csv(trades_path, TRADE_REPORT_FIELDS, payload["trade_report_rows"])
        output_paths = [summary_path, trades_path]

    return TaxExportResult(
        format=export_format,
        year=year,
        market=normalized_market,
        output_paths=output_paths,
        yearly_summary=payload["yearly_summary"],
        trade_report_rows=payload["trade_report_rows"],
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = export_tax_report(
        year=args.year,
        market=args.market,
        output_format=args.format,
        output_dir=args.output_dir,
        output_stem=args.output_stem,
    )
    for path in result.output_paths:
        print(path)
    return 0


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export yearly tax summary and trade-level report.")
    parser.add_argument("--year", type=int, required=True, help="Target tax year.")
    parser.add_argument("--market", choices=["KR", "US"], help="Optional market filter.")
    parser.add_argument("--format", choices=["json", "csv"], default="json", help="Export format.")
    parser.add_argument(
        "--output-dir",
        default=str(Path("reports") / "tax"),
        help="Destination directory for exported report files.",
    )
    parser.add_argument("--output-stem", help="Optional filename stem without extension.")
    return parser.parse_args(argv)


def _default_output_stem(*, year: int, market: str | None) -> str:
    market_label = "all" if market is None else market.lower()
    return f"tax_report_{year}_{market_label}"


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "yearly_summary": _normalize_mapping(payload["yearly_summary"]),
        "trade_report_rows": [_normalize_mapping(row) for row in payload["trade_report_rows"]],
    }


def _normalize_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            normalized[key] = value.isoformat()
        elif isinstance(value, dict):
            normalized[key] = _normalize_mapping(value)
        else:
            normalized[key] = value
    return normalized


def _build_summary_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        _summary_row(
            scope="total",
            market=summary.get("market"),
            year=int(summary["year"]),
            payload=summary,
        )
    ]
    by_market = summary.get("by_market") or {}
    for market_key, market_payload in sorted(by_market.items()):
        if not isinstance(market_payload, dict):
            continue
        rows.append(
            _summary_row(
                scope="by_market",
                market=market_key,
                year=int(summary["year"]),
                payload=market_payload,
            )
        )
    return rows


def _summary_row(*, scope: str, market: str | None, year: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": scope,
        "market": market or "all",
        "year": year,
        "sell_trade_count": payload.get("sell_trade_count", 0),
        "total_quantity": payload.get("total_quantity", 0),
        "realized_gain_loss_krw": payload.get("realized_gain_loss_krw", 0.0),
        "taxable_gain_krw": payload.get("taxable_gain_krw", 0.0),
        "total_fees_krw": payload.get("total_fees_krw", 0.0),
        "total_taxes_krw": payload.get("total_taxes_krw", 0.0),
    }


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _serialize_csv_value(row.get(key)) for key in fieldnames})


def _serialize_csv_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value
