from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backtest.backtest_runner import BacktestRunResult, BacktestRunner
from core.models import FactorSnapshot, PriceBar
from core.settings import Settings, get_settings
from data.collector import build_pykrx_price_history_loader
from data.database import init_db
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder


KST = timezone(timedelta(hours=9))
SUPPORTED_STRATEGIES = {"dual_momentum", "factor_investing"}
CSV_FACTOR_REQUIRED_FIELDS = {
    "date",
    "ticker",
    "value_score",
    "quality_score",
    "momentum_score",
    "low_vol_score",
}


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_kst_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST).date()
    return value.astimezone(KST).date()


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if len(normalized) == 10:
        parsed = datetime.strptime(normalized, "%Y-%m-%d")
        return parsed.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return _coerce_utc(parsed)


def _parse_ticker_list(*, tickers: str | None, tickers_file: Path | None) -> list[str]:
    if tickers and tickers_file is not None:
        raise ValueError("use either --tickers or --tickers-file, not both")
    if not tickers and tickers_file is None:
        raise ValueError("one of --tickers or --tickers-file is required")

    raw_values: list[str]
    if tickers:
        raw_values = tickers.split(",")
    else:
        content = tickers_file.read_text(encoding="utf-8")
        raw_values = content.replace("\n", ",").split(",")

    parsed = [value.strip().upper() for value in raw_values if value.strip()]
    deduped = list(dict.fromkeys(parsed))
    if not deduped:
        raise ValueError("ticker universe must not be empty")
    return deduped


def _parse_factor_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError("factor file CSV must include a header row")
            missing_fields = sorted(CSV_FACTOR_REQUIRED_FIELDS - set(reader.fieldnames))
            if missing_fields:
                raise ValueError(
                    "factor file CSV is missing required fields: " + ", ".join(missing_fields)
                )
            return [dict(row) for row in reader]

    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            rows = payload["rows"]
        else:
            raise ValueError("factor file JSON must be a list of rows or an object with a rows list")
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("factor file JSON rows must be objects")
        return [dict(row) for row in rows]

    raise ValueError("factor file must use .csv or .json")


def build_factor_file_loader(path: Path):
    rows = _parse_factor_rows(path)
    snapshots_by_date: dict[date, dict[str, FactorSnapshot]] = {}

    for row in rows:
        factor_date_value = row.get("date") or row.get("as_of_date")
        if not factor_date_value:
            raise ValueError("factor rows must include date")
        factor_date = datetime.strptime(str(factor_date_value), "%Y-%m-%d").date()
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            raise ValueError("factor rows must include ticker")
        market = str(row.get("market", "KR")).strip().upper()
        if market != "KR":
            continue

        day_bucket = snapshots_by_date.setdefault(factor_date, {})
        if ticker in day_bucket:
            raise ValueError(f"duplicate factor snapshot for {ticker} on {factor_date.isoformat()}")
        day_bucket[ticker] = FactorSnapshot(
            ticker=ticker,
            market="KR",
            value_score=float(row["value_score"]),
            quality_score=float(row["quality_score"]),
            momentum_score=float(row["momentum_score"]),
            low_vol_score=float(row["low_vol_score"]),
        )

    available_dates = sorted(snapshots_by_date)
    if not available_dates:
        raise ValueError("factor file did not yield any KR snapshots")

    def loader(tickers: list[str], market: str, as_of: datetime) -> dict[str, FactorSnapshot]:
        if market != "KR":
            return {}
        target_date = _coerce_kst_date(as_of)
        candidate_dates = [value for value in available_dates if value <= target_date]
        if not candidate_dates:
            return {}
        selected_date = candidate_dates[-1]
        snapshots = snapshots_by_date[selected_date]
        return {
            ticker: snapshots[ticker]
            for ticker in dict.fromkeys([value.upper() for value in tickers])
            if ticker in snapshots
        }

    return loader


class PreloadedKRBacktestDataProvider:
    def __init__(
        self,
        *,
        universe: list[str],
        start_date: datetime,
        end_date: datetime,
        price_history_loader,
        factor_input_loader=None,
    ) -> None:
        self.factor_input_loader = factor_input_loader
        self._market = "KR"
        self._start_date = _coerce_utc(start_date)
        self._end_date = _coerce_utc(end_date)
        self._universe = list(dict.fromkeys([ticker.upper() for ticker in universe]))
        requested_lookback = max((self._end_date.date() - self._start_date.date()).days + 370, 400)
        raw_histories = price_history_loader(self._universe, self._end_date, requested_lookback)
        self._histories = self._normalize_histories(raw_histories)

    def _normalize_histories(self, raw_histories: dict[str, list[dict[str, object]]]) -> dict[str, list[PriceBar]]:
        histories: dict[str, list[PriceBar]] = {}
        for ticker in self._universe:
            bars: list[PriceBar] = []
            for raw_bar in raw_histories.get(ticker, []):
                if isinstance(raw_bar, PriceBar):
                    bar = PriceBar(
                        ticker=ticker,
                        market=self._market,
                        timestamp=_coerce_utc(raw_bar.timestamp),
                        close=float(raw_bar.close),
                        high=float(raw_bar.high) if raw_bar.high is not None else None,
                        low=float(raw_bar.low) if raw_bar.low is not None else None,
                    )
                else:
                    timestamp = raw_bar.get("timestamp")
                    close = raw_bar.get("close")
                    if not isinstance(timestamp, datetime) or close is None:
                        continue
                    bar = PriceBar(
                        ticker=ticker,
                        market=self._market,
                        timestamp=_coerce_utc(timestamp),
                        close=float(close),
                        high=float(raw_bar["high"]) if raw_bar.get("high") is not None else None,
                        low=float(raw_bar["low"]) if raw_bar.get("low") is not None else None,
                    )
                if bar.timestamp <= self._end_date:
                    bars.append(bar)
            bars.sort(key=lambda value: value.timestamp)
            if bars:
                histories[ticker] = bars
        return histories

    def get_price_history(
        self,
        tickers: list[str],
        market: str,
        as_of: datetime,
        lookback_days: int,
    ) -> dict[str, list[PriceBar]]:
        if market != self._market:
            return {}
        as_of_utc = _coerce_utc(as_of)
        histories: dict[str, list[PriceBar]] = {}
        for ticker in dict.fromkeys([value.upper() for value in tickers]):
            bars = [bar for bar in self._histories.get(ticker, []) if bar.timestamp <= as_of_utc]
            if bars:
                histories[ticker] = bars[-lookback_days:]
        return histories

    def get_factor_inputs(
        self,
        tickers: list[str],
        market: str,
        as_of: datetime,
    ) -> dict[str, FactorSnapshot]:
        if self.factor_input_loader is None:
            return {}
        return self.factor_input_loader(tickers, market, as_of)

    def get_event_flags(self, tickers, market, as_of):
        del tickers, market, as_of
        return []


def run_kr_rebalance_backtest(
    *,
    strategy: str,
    start_date: datetime,
    end_date: datetime,
    universe: list[str],
    factor_file: Path | None = None,
    initial_capital: float = 1_000_000.0,
    persist: bool = False,
    settings: Settings | None = None,
    price_history_loader=None,
    factor_input_loader=None,
) -> BacktestRunResult:
    strategy_name = strategy.strip()
    if strategy_name not in SUPPORTED_STRATEGIES:
        raise ValueError("strategy must be dual_momentum or factor_investing")

    runtime_settings = settings or get_settings()
    resolved_price_history_loader = price_history_loader or build_pykrx_price_history_loader()
    resolved_factor_input_loader = factor_input_loader
    if resolved_factor_input_loader is None and factor_file is not None:
        resolved_factor_input_loader = build_factor_file_loader(factor_file)
    if strategy_name == "factor_investing" and resolved_factor_input_loader is None:
        raise ValueError("factor_investing backtests require --factor-file or a custom factor_input_loader")

    data_provider = PreloadedKRBacktestDataProvider(
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        price_history_loader=resolved_price_history_loader,
        factor_input_loader=resolved_factor_input_loader,
    )
    if not data_provider.get_price_history(universe, "KR", end_date, 1):
        raise ValueError("no KR price history could be loaded for the requested universe")

    writer_queue: WriterQueue | None = None
    operations_recorder: OperationsRecorder | None = None
    created_writer_queue = False
    try:
        if persist:
            init_db(runtime_settings)
            writer_queue = WriterQueue.from_settings(runtime_settings)
            writer_queue.start()
            created_writer_queue = True
            operations_recorder = OperationsRecorder(writer_queue)

        runner = BacktestRunner(
            data_provider=data_provider,
            writer_queue=writer_queue,
            operations_recorder=operations_recorder,
            settings=runtime_settings,
        )
        return runner.run(
            strategy_name,
            "KR",
            start_date,
            end_date,
            universe=universe,
            initial_capital=initial_capital,
            persist=persist,
        )
    finally:
        if created_writer_queue and writer_queue is not None:
            writer_queue.stop()


def _result_to_payload(
    result: BacktestRunResult,
    *,
    universe: list[str],
    factor_file: Path | None,
    persist: bool,
) -> dict[str, Any]:
    return {
        "strategy": result.strategy,
        "market": result.market,
        "start_date": result.start_date.isoformat(),
        "end_date": result.end_date.isoformat(),
        "annual_return": result.annual_return,
        "sharpe_ratio": result.sharpe_ratio,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "total_trades": result.total_trades,
        "profit_factor": result.profit_factor,
        "engine": result.engine,
        "persisted": persist,
        "backtest_result_id": result.backtest_result_id,
        "universe_count": len(universe),
        "universe": universe,
        "factor_file": None if factor_file is None else str(factor_file),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run KR dual_momentum/factor_investing backtests")
    parser.add_argument("--strategy", choices=sorted(SUPPORTED_STRATEGIES), required=True)
    parser.add_argument("--start-date", required=True, help="Backtest start date in YYYY-MM-DD or ISO8601")
    parser.add_argument("--end-date", required=True, help="Backtest end date in YYYY-MM-DD or ISO8601")
    parser.add_argument("--tickers", help="Comma-separated KR tickers")
    parser.add_argument("--tickers-file", type=Path, help="Path to a text file containing KR tickers")
    parser.add_argument("--factor-file", type=Path, help="CSV/JSON factor snapshot file for factor_investing")
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--persist", action="store_true", help="Persist results to backtest_results and system_logs")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    universe = _parse_ticker_list(tickers=args.tickers, tickers_file=args.tickers_file)
    start_date = _parse_datetime(args.start_date)
    end_date = _parse_datetime(args.end_date)
    factor_file = args.factor_file
    result = run_kr_rebalance_backtest(
        strategy=args.strategy,
        start_date=start_date,
        end_date=end_date,
        universe=universe,
        factor_file=factor_file,
        initial_capital=args.initial_capital,
        persist=args.persist,
        settings=get_settings(),
    )
    print(json.dumps(_result_to_payload(result, universe=universe, factor_file=factor_file, persist=args.persist), indent=2))


if __name__ == "__main__":
    main()
