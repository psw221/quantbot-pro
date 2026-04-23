from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.run_kr_rebalance_backtest import (
    build_factor_file_loader,
    run_kr_rebalance_backtest,
)
from tests.test_execution.test_bootstrap import build_settings


def _bars(ticker: str, closes: list[float], *, start: datetime | None = None):
    base = start or datetime(2025, 1, 1, tzinfo=UTC)
    return [
        {
            "timestamp": base + timedelta(days=index),
            "close": close,
            "high": close + 1,
            "low": close - 1,
        }
        for index, close in enumerate(closes)
    ]


def test_factor_file_loader_uses_latest_snapshot_on_or_before_rebalance_date(tmp_path: Path) -> None:
    factor_file = tmp_path / "factors.csv"
    factor_file.write_text(
        "\n".join(
            [
                "date,ticker,market,value_score,quality_score,momentum_score,low_vol_score",
                "2026-03-31,AAA,KR,0.9,0.8,0.7,0.6",
                "2026-03-31,BBB,KR,0.5,0.5,0.5,0.5",
                "2026-04-30,AAA,KR,0.1,0.1,0.1,0.1",
            ]
        ),
        encoding="utf-8",
    )
    loader = build_factor_file_loader(factor_file)

    result = loader(["AAA", "BBB"], "KR", datetime(2026, 4, 1, tzinfo=UTC))

    assert sorted(result) == ["AAA", "BBB"]
    assert result["AAA"].value_score == 0.9
    assert result["BBB"].momentum_score == 0.5


def test_run_kr_rebalance_backtest_runs_dual_momentum_with_injected_price_loader(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    histories = {
        "AAA": _bars("AAA", [100 + index for index in range(500)], start=start),
        "BBB": _bars("BBB", [100 + (index * 0.2) for index in range(500)], start=start),
    }

    result = run_kr_rebalance_backtest(
        strategy="dual_momentum",
        start_date=datetime(2026, 5, 1, tzinfo=UTC),
        end_date=datetime(2026, 5, 10, tzinfo=UTC),
        universe=["AAA", "BBB"],
        persist=False,
        settings=settings,
        price_history_loader=lambda tickers, as_of, lookback_days: {ticker: histories[ticker] for ticker in tickers},
    )

    assert result.strategy == "dual_momentum"
    assert result.market == "KR"
    assert result.total_trades >= 1
    assert result.engine in {"vectorbt", "fallback"}


def test_run_kr_rebalance_backtest_requires_factor_source_for_factor_strategy(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    histories = {"AAA": _bars("AAA", [100 + index for index in range(500)])}

    with pytest.raises(ValueError, match="factor_investing backtests require"):
        run_kr_rebalance_backtest(
            strategy="factor_investing",
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 10, tzinfo=UTC),
            universe=["AAA"],
            persist=False,
            settings=settings,
            price_history_loader=lambda tickers, as_of, lookback_days: {ticker: histories[ticker] for ticker in tickers},
        )


def test_run_kr_rebalance_backtest_runs_factor_strategy_with_factor_file(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    factor_file = tmp_path / "factors.csv"
    factor_file.write_text(
        "\n".join(
            [
                "date,ticker,market,value_score,quality_score,momentum_score,low_vol_score",
                "2026-03-31,AAA,KR,1.0,1.0,1.0,1.0",
                "2026-03-31,BBB,KR,0.1,0.1,0.1,0.1",
            ]
        ),
        encoding="utf-8",
    )
    start = datetime(2025, 1, 1, tzinfo=UTC)
    histories = {
        "AAA": _bars("AAA", [100 + index for index in range(500)], start=start),
        "BBB": _bars("BBB", [100 + (index * 0.1) for index in range(500)], start=start),
    }

    result = run_kr_rebalance_backtest(
        strategy="factor_investing",
        start_date=datetime(2026, 4, 1, tzinfo=UTC),
        end_date=datetime(2026, 4, 10, tzinfo=UTC),
        universe=["AAA", "BBB"],
        factor_file=factor_file,
        persist=False,
        settings=settings,
        price_history_loader=lambda tickers, as_of, lookback_days: {ticker: histories[ticker] for ticker in tickers},
    )

    assert result.strategy == "factor_investing"
    assert result.total_trades >= 1
    assert result.engine in {"vectorbt", "fallback"}
