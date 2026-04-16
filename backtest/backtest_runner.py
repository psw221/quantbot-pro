from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from math import sqrt

from core.settings import Settings, get_settings
from data.database import BacktestResult, utc_now
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder
from strategy.base import StrategyDataProvider
from strategy.dual_momentum import DualMomentumStrategy
from strategy.factor_investing import FactorInvestingStrategy
from strategy.trend_following import TrendFollowingStrategy


STRATEGY_BUILDERS = {
    "dual_momentum": lambda settings, provider: DualMomentumStrategy(settings.strategies.dual_momentum, data_provider=provider),
    "trend_following": lambda settings, provider: TrendFollowingStrategy(settings.strategies.trend_following, data_provider=provider),
    "factor_investing": lambda settings, provider: FactorInvestingStrategy(settings.strategies.factor_investing, data_provider=provider),
}


@dataclass(slots=True)
class BacktestRunResult:
    strategy: str
    market: str
    start_date: datetime
    end_date: datetime
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    profit_factor: float
    engine: str
    backtest_result_id: int | None = None


class BacktestRunner:
    def __init__(
        self,
        *,
        data_provider: StrategyDataProvider,
        writer_queue: WriterQueue | None = None,
        operations_recorder: OperationsRecorder | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.data_provider = data_provider
        self.writer_queue = writer_queue
        self.operations_recorder = operations_recorder
        self.settings = settings or get_settings()

    def run(
        self,
        strategy: str,
        market: str,
        start_date: datetime,
        end_date: datetime,
        *,
        universe: list[str],
        initial_capital: float = 1_000_000.0,
        persist: bool = True,
    ) -> BacktestRunResult:
        strategy_name = strategy.strip()
        if strategy_name not in STRATEGY_BUILDERS:
            raise ValueError(f"unsupported strategy: {strategy_name}")
        if end_date <= start_date:
            raise ValueError("end_date must be after start_date")
        if not universe:
            raise ValueError("universe must not be empty")
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")

        timestamps, close_map = self._build_close_map(universe, market, start_date, end_date)
        if not timestamps:
            raise ValueError("no price history available for requested range")

        entries, exits = self._build_signal_schedule(strategy_name, market, start_date, end_date, universe, timestamps, close_map)
        metrics = self._run_engine(timestamps, close_map, entries, exits, initial_capital=initial_capital)
        result = BacktestRunResult(
            strategy=strategy_name,
            market=market,
            start_date=start_date,
            end_date=end_date,
            annual_return=metrics["annual_return"],
            sharpe_ratio=metrics["sharpe_ratio"],
            max_drawdown=metrics["max_drawdown"],
            win_rate=metrics["win_rate"],
            total_trades=metrics["total_trades"],
            profit_factor=metrics["profit_factor"],
            engine=str(metrics.get("engine", "fallback")),
        )

        if persist:
            if self.writer_queue is None:
                raise ValueError("writer_queue is required when persist=True")
            result.backtest_result_id = self._persist_result(
                result,
                params={
                    "universe": universe,
                    "initial_capital": initial_capital,
                },
            )
            if self.operations_recorder is not None:
                self.operations_recorder.record_system_log(
                    level="INFO",
                    module="backtest.backtest_runner",
                    message="backtest completed",
                    extra={
                        "strategy": strategy_name,
                        "market": market,
                        "engine": result.engine,
                        "backtest_result_id": result.backtest_result_id,
                    },
                )

        return result

    def _build_close_map(
        self,
        universe: list[str],
        market: str,
        start_date: datetime,
        end_date: datetime,
    ) -> tuple[list[datetime], dict[str, dict[datetime, float]]]:
        lookback_days = max((end_date.date() - start_date.date()).days + 370, 400)
        histories = self.data_provider.get_price_history(universe, market, end_date, lookback_days)
        timestamps: set[datetime] = set()
        close_map: dict[str, dict[datetime, float]] = {ticker: {} for ticker in universe}

        for ticker, bars in histories.items():
            for bar in bars:
                timestamp = _coerce_utc(bar.timestamp)
                if start_date <= timestamp <= end_date:
                    close_map.setdefault(ticker, {})[timestamp] = float(bar.close)
                    timestamps.add(timestamp)

        return sorted(timestamps), close_map

    def _build_signal_schedule(
        self,
        strategy_name: str,
        market: str,
        start_date: datetime,
        end_date: datetime,
        universe: list[str],
        timestamps: list[datetime],
        close_map: dict[str, dict[datetime, float]],
    ) -> tuple[dict[datetime, set[str]], dict[datetime, set[str]]]:
        strategy = STRATEGY_BUILDERS[strategy_name](self.settings, self.data_provider)
        entries = {timestamp: set() for timestamp in timestamps}
        exits = {timestamp: set() for timestamp in timestamps}
        open_positions = {ticker: False for ticker in universe}

        for timestamp in timestamps:
            if timestamp < start_date or timestamp > end_date:
                continue
            signals = strategy.generate_signals(universe, market, timestamp)
            for signal in signals:
                close_price = close_map.get(signal.ticker, {}).get(timestamp)
                if close_price is None:
                    continue
                if signal.action == "buy" and not open_positions.get(signal.ticker, False):
                    entries[timestamp].add(signal.ticker)
                    open_positions[signal.ticker] = True
                elif signal.action == "sell" and open_positions.get(signal.ticker, False):
                    exits[timestamp].add(signal.ticker)
                    open_positions[signal.ticker] = False

        final_timestamp = timestamps[-1]
        for ticker, is_open in open_positions.items():
            if is_open and close_map.get(ticker, {}).get(final_timestamp) is not None:
                exits[final_timestamp].add(ticker)

        return entries, exits

    def _run_fallback(
        self,
        timestamps: list[datetime],
        close_map: dict[str, dict[datetime, float]],
        entries: dict[datetime, set[str]],
        exits: dict[datetime, set[str]],
    ) -> dict[str, float | int]:
        open_trades: dict[str, float] = {}
        realized_returns: list[float] = []
        realized_by_date: dict[datetime, float] = {}

        for timestamp in timestamps:
            for ticker, ticker_prices in close_map.items():
                close_price = ticker_prices.get(timestamp)
                if close_price is None:
                    continue
                if ticker in entries.get(timestamp, set()) and ticker not in open_trades:
                    open_trades[ticker] = close_price
                if ticker in exits.get(timestamp, set()) and ticker in open_trades:
                    entry_price = open_trades.pop(ticker)
                    trade_return = (close_price - entry_price) / entry_price if entry_price else 0.0
                    realized_returns.append(trade_return)
                    realized_by_date[timestamp] = realized_by_date.get(timestamp, 0.0) + trade_return

        if not realized_returns:
            return {
                "annual_return": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
                "total_trades": 0,
                "profit_factor": 0.0,
            }

        wins = [value for value in realized_returns if value > 0]
        losses = [value for value in realized_returns if value < 0]
        profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
        ordered_dates = sorted(realized_by_date)

        equity_points: list[float] = []
        equity = 1.0
        for date in ordered_dates:
            equity *= 1 + realized_by_date[date]
            equity_points.append(equity)

        total_return = equity_points[-1] - 1
        trading_days = max((timestamps[-1] - timestamps[0]).days, 1)
        annual_factor = 365 / trading_days
        annual_return = (1 + total_return) ** annual_factor - 1 if total_return > -1 else -1.0

        sharpe_ratio = 0.0
        if len(ordered_dates) > 1:
            daily_returns = [realized_by_date[date] for date in ordered_dates]
            mean_return = sum(daily_returns) / len(daily_returns)
            variance = sum((value - mean_return) ** 2 for value in daily_returns) / len(daily_returns)
            std_return = variance ** 0.5
            if std_return > 0:
                sharpe_ratio = (mean_return / std_return) * sqrt(252)

        running_max = 1.0
        max_drawdown = 0.0
        for equity in equity_points:
            running_max = max(running_max, equity)
            max_drawdown = min(max_drawdown, (equity / running_max) - 1)

        return {
            "annual_return": float(annual_return),
            "sharpe_ratio": float(sharpe_ratio),
            "max_drawdown": float(max_drawdown),
            "win_rate": len(wins) / len(realized_returns),
            "total_trades": len(realized_returns),
            "profit_factor": float(profit_factor),
        }

    def _run_engine(
        self,
        timestamps: list[datetime],
        close_map: dict[str, dict[datetime, float]],
        entries: dict[datetime, set[str]],
        exits: dict[datetime, set[str]],
        *,
        initial_capital: float,
    ) -> dict[str, float | int | str]:
        try:
            import pandas as pd  # type: ignore
            import vectorbt as vbt  # type: ignore

            price_frame = pd.DataFrame(
                {
                    ticker: [close_map.get(ticker, {}).get(timestamp) for timestamp in timestamps]
                    for ticker in sorted(close_map)
                },
                index=timestamps,
            )
            entries_frame = pd.DataFrame(
                {
                    ticker: [ticker in entries.get(timestamp, set()) for timestamp in timestamps]
                    for ticker in sorted(close_map)
                },
                index=timestamps,
            )
            exits_frame = pd.DataFrame(
                {
                    ticker: [ticker in exits.get(timestamp, set()) for timestamp in timestamps]
                    for ticker in sorted(close_map)
                },
                index=timestamps,
            )
            portfolio = vbt.Portfolio.from_signals(
                price_frame,
                entries_frame,
                exits_frame,
                freq="1D",
                init_cash=initial_capital,
                cash_sharing=True,
            )
            trades = portfolio.trades
            return {
                "annual_return": float(_to_scalar(trades.wrapper.wrap_reduced(portfolio.annualized_return()))),
                "sharpe_ratio": float(_to_scalar(trades.wrapper.wrap_reduced(portfolio.sharpe_ratio()))),
                "max_drawdown": float(_to_scalar(trades.wrapper.wrap_reduced(portfolio.max_drawdown()))),
                "win_rate": float(_to_scalar(trades.win_rate())),
                "total_trades": int(_to_scalar(trades.count())),
                "profit_factor": float(_to_scalar(trades.profit_factor())),
                "engine": "vectorbt",
            }
        except Exception:
            metrics = self._run_fallback(timestamps, close_map, entries, exits)
            return {**metrics, "engine": "fallback"}

    def _persist_result(self, result: BacktestRunResult, *, params: dict[str, object]) -> int:
        assert self.writer_queue is not None
        future = self.writer_queue.submit(
            lambda session: self._insert_backtest_result(session, result, params=params),
            description="insert backtest result",
        )
        return future.result()

    @staticmethod
    def _insert_backtest_result(session, result: BacktestRunResult, *, params: dict[str, object]) -> int:
        row = BacktestResult(
            strategy=result.strategy,
            market=result.market,
            start_date=result.start_date,
            end_date=result.end_date,
            params_json=json.dumps(params, sort_keys=True, default=str),
            annual_return=result.annual_return,
            sharpe_ratio=result.sharpe_ratio,
            max_drawdown=result.max_drawdown,
            win_rate=result.win_rate,
            total_trades=result.total_trades,
            profit_factor=result.profit_factor,
            notes=f"engine={result.engine}",
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        return row.id


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_scalar(value: object) -> float:
    if hasattr(value, "item"):
        return float(value.item())  # type: ignore[call-arg]
    return float(value)  # type: ignore[arg-type]
