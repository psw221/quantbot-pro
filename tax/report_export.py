from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from tax.tax_calculator import TaxCalculator


SUMMARY_FIELDS = [
    "report_scope",
    "scope",
    "market",
    "year",
    "month",
    "period_start",
    "period_end",
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
    report_scope: str
    year: int
    month: int | None
    market: str | None
    output_paths: list[Path]
    yearly_summary: dict[str, Any]
    period_summary: dict[str, Any] | None
    trade_report_rows: list[dict[str, Any]]


def build_tax_export_payload(
    calculator: TaxCalculator,
    *,
    year: int,
    month: int | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    normalized_market = None if market is None else market.upper()
    if month is not None and month not in range(1, 13):
        raise ValueError("month must be between 1 and 12")

    yearly_summary = calculator.calculate_yearly_summary(year, market=normalized_market)
    trade_report_rows = calculator.build_trade_report(year, market=normalized_market)
    if month is None:
        return {
            "report_scope": "yearly",
            "yearly_summary": yearly_summary,
            "period_summary": None,
            "trade_report_rows": trade_report_rows,
        }

    filtered_rows = _filter_trade_rows_by_month(trade_report_rows, year=year, month=month)
    return {
        "report_scope": "monthly",
        "yearly_summary": yearly_summary,
        "period_summary": _build_period_summary(filtered_rows, year=year, month=month, market=normalized_market),
        "trade_report_rows": filtered_rows,
    }


def export_tax_report(
    *,
    year: int,
    month: int | None = None,
    market: str | None = None,
    output_format: str = "json",
    output_dir: str | Path = Path("reports") / "tax",
    output_stem: str | None = None,
    calculator: TaxCalculator | None = None,
) -> TaxExportResult:
    normalized_market = None if market is None else market.upper()
    if normalized_market not in (None, "KR", "US"):
        raise ValueError("market must be KR, US, or omitted")
    if month is not None and month not in range(1, 13):
        raise ValueError("month must be between 1 and 12")

    export_format = output_format.lower()
    if export_format not in {"json", "csv"}:
        raise ValueError("output_format must be json or csv")

    export_calculator = calculator or TaxCalculator()
    payload = build_tax_export_payload(export_calculator, year=year, month=month, market=normalized_market)
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    base_name = output_stem or _default_output_stem(year=year, month=month, market=normalized_market)

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
        summary_source = payload["period_summary"] or payload["yearly_summary"]
        _write_csv(summary_path, SUMMARY_FIELDS, _build_summary_rows(summary_source, report_scope=payload["report_scope"]))
        _write_csv(trades_path, TRADE_REPORT_FIELDS, payload["trade_report_rows"])
        output_paths = [summary_path, trades_path]

    return TaxExportResult(
        format=export_format,
        report_scope=str(payload["report_scope"]),
        year=year,
        month=month,
        market=normalized_market,
        output_paths=output_paths,
        yearly_summary=payload["yearly_summary"],
        period_summary=payload["period_summary"],
        trade_report_rows=payload["trade_report_rows"],
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = export_tax_report(
        year=args.year,
        month=args.month,
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
    parser.add_argument("--month", type=int, choices=range(1, 13), help="Optional target month for a periodic report.")
    parser.add_argument("--market", choices=["KR", "US"], help="Optional market filter.")
    parser.add_argument("--format", choices=["json", "csv"], default="json", help="Export format.")
    parser.add_argument(
        "--output-dir",
        default=str(Path("reports") / "tax"),
        help="Destination directory for exported report files.",
    )
    parser.add_argument("--output-stem", help="Optional filename stem without extension.")
    return parser.parse_args(argv)


def _default_output_stem(*, year: int, month: int | None, market: str | None) -> str:
    market_label = "all" if market is None else market.lower()
    if month is None:
        return f"tax_report_{year}_{market_label}"
    return f"tax_report_{year}_{month:02d}_{market_label}"


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_scope": payload["report_scope"],
        "yearly_summary": _normalize_mapping(payload["yearly_summary"]),
        "period_summary": None if payload["period_summary"] is None else _normalize_mapping(payload["period_summary"]),
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


def _build_summary_rows(summary: dict[str, Any], *, report_scope: str) -> list[dict[str, Any]]:
    rows = [
        _summary_row(
            report_scope=report_scope,
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
                report_scope=report_scope,
                scope="by_market",
                market=market_key,
                year=int(summary["year"]),
                payload=market_payload,
            )
        )
    return rows


def _summary_row(*, report_scope: str, scope: str, market: str | None, year: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_scope": report_scope,
        "scope": scope,
        "market": market or "all",
        "year": year,
        "month": payload.get("month"),
        "period_start": payload.get("period_start"),
        "period_end": payload.get("period_end"),
        "sell_trade_count": payload.get("sell_trade_count", 0),
        "total_quantity": payload.get("total_quantity", 0),
        "realized_gain_loss_krw": payload.get("realized_gain_loss_krw", 0.0),
        "taxable_gain_krw": payload.get("taxable_gain_krw", 0.0),
        "total_fees_krw": payload.get("total_fees_krw", 0.0),
        "total_taxes_krw": payload.get("total_taxes_krw", 0.0),
    }


def _filter_trade_rows_by_month(rows: list[dict[str, Any]], *, year: int, month: int) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        sell_date = _coerce_datetime(row.get("sell_date"))
        if sell_date is None:
            continue
        if sell_date.year == year and sell_date.month == month:
            filtered.append(row)
    return filtered


def _build_period_summary(
    rows: list[dict[str, Any]],
    *,
    year: int,
    month: int,
    market: str | None,
) -> dict[str, Any]:
    by_market: dict[str, dict[str, Any]] = {}
    totals = {
        "year": year,
        "month": month,
        "market": market,
        "period_start": f"{year:04d}-{month:02d}-01",
        "period_end": _period_end_label(year, month),
        "sell_trade_count": 0,
        "total_quantity": 0,
        "realized_gain_loss_krw": 0.0,
        "taxable_gain_krw": 0.0,
        "total_fees_krw": 0.0,
        "total_taxes_krw": 0.0,
        "by_market": by_market,
    }

    for row in rows:
        market_key = str(row.get("market") or market or "UNKNOWN")
        market_summary = by_market.setdefault(
            market_key,
            {
                "year": year,
                "month": month,
                "market": market_key,
                "period_start": totals["period_start"],
                "period_end": totals["period_end"],
                "sell_trade_count": 0,
                "total_quantity": 0,
                "realized_gain_loss_krw": 0.0,
                "taxable_gain_krw": 0.0,
                "total_fees_krw": 0.0,
                "total_taxes_krw": 0.0,
            },
        )
        _accumulate_period_row(totals, row)
        _accumulate_period_row(market_summary, row)

    return totals


def _accumulate_period_row(summary: dict[str, Any], row: dict[str, Any]) -> None:
    summary["sell_trade_count"] += 1
    summary["total_quantity"] += int(row.get("quantity") or 0)
    summary["realized_gain_loss_krw"] += float(row.get("realized_gain_loss_krw") or 0.0)
    summary["taxable_gain_krw"] += float(row.get("taxable_gain_krw") or 0.0)
    summary["total_fees_krw"] += float(row.get("fee_krw") or 0.0)
    summary["total_taxes_krw"] += float(row.get("tax_krw") or 0.0)


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _period_end_label(year: int, month: int) -> str:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return (next_month.replace(day=1) - timedelta(days=1)).date().isoformat()


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
