from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.models import FactorSnapshot, PositionSnapshot, PriceBar
from strategy.dual_momentum import DualMomentumStrategy
from strategy.factor_investing import FactorInvestingStrategy
from strategy.trend_following import TrendFollowingStrategy
from tests.test_execution.test_bootstrap import build_settings


class FakeStrategyDataProvider:
    def __init__(
        self,
        *,
        prices: dict[str, list[PriceBar]] | None = None,
        factors: dict[str, FactorSnapshot] | None = None,
    ) -> None:
        self.prices = prices or {}
        self.factors = factors or {}

    def get_price_history(self, tickers, market, as_of, lookback_days):
        return {ticker: self.prices.get(ticker, []) for ticker in tickers}

    def get_intraday_bars(self, tickers, market, as_of, lookback_minutes):
        return {}

    def get_factor_inputs(self, tickers, market, as_of):
        return {ticker: self.factors[ticker] for ticker in tickers if ticker in self.factors}

    def get_event_flags(self, tickers, market, as_of):
        return []


def _bars(ticker: str, market: str, closes: list[float]) -> list[PriceBar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[PriceBar] = []
    for index, close in enumerate(closes):
        bars.append(
            PriceBar(
                ticker=ticker,
                market=market,
                timestamp=start + timedelta(days=index),
                close=close,
                high=close + 1,
                low=close - 1,
            )
        )
    return bars


def test_dual_momentum_generates_monthly_top_n_signals(tmp_path) -> None:
    settings = build_settings(tmp_path)
    provider = FakeStrategyDataProvider(
        prices={
            "AAA": _bars("AAA", "KR", [100, 140]),
            "BBB": _bars("BBB", "KR", [100, 120]),
            "CCC": _bars("CCC", "KR", [100, 90]),
        }
    )
    config = settings.strategies.dual_momentum.model_copy(update={"top_n": 2})
    strategy = DualMomentumStrategy(config, data_provider=provider)

    signals = strategy.generate_signals(["AAA", "BBB", "CCC"], "KR", datetime(2026, 5, 1, tzinfo=UTC))

    assert [(signal.ticker, signal.action) for signal in signals] == [
        ("AAA", "buy"),
        ("BBB", "buy"),
        ("CCC", "sell"),
    ]


def test_dual_momentum_skips_non_rebalance_day(tmp_path) -> None:
    settings = build_settings(tmp_path)
    strategy = DualMomentumStrategy(settings.strategies.dual_momentum, data_provider=FakeStrategyDataProvider())

    signals = strategy.generate_signals(["AAA"], "KR", datetime(2026, 5, 2, tzinfo=UTC))

    assert signals == []


def test_trend_following_generates_buy_signal_for_uptrend(tmp_path) -> None:
    settings = build_settings(tmp_path)
    closes = [100 + index for index in range(70)]
    provider = FakeStrategyDataProvider(prices={"AAPL": _bars("AAPL", "US", closes)})
    strategy = TrendFollowingStrategy(settings.strategies.trend_following, data_provider=provider)

    signals = strategy.generate_signals(["AAPL"], "US", datetime(2026, 5, 1, tzinfo=UTC))

    assert len(signals) == 1
    assert signals[0].action == "buy"
    assert signals[0].metadata["target_vol"] == settings.strategies.trend_following.target_volatility


def test_trend_following_exit_signal_uses_trailing_or_atr_rules(tmp_path) -> None:
    settings = build_settings(tmp_path)
    closes = [100 + index for index in range(30)] + [120, 118, 117, 115, 112, 108, 104, 100]
    provider = FakeStrategyDataProvider(prices={"AAPL": _bars("AAPL", "US", closes)})
    strategy = TrendFollowingStrategy(settings.strategies.trend_following, data_provider=provider)
    position = PositionSnapshot(
        ticker="AAPL",
        market="US",
        strategy="trend_following",
        quantity=5,
        avg_cost=100.0,
        current_price=100.0,
        highest_price=130.0,
        entry_date=datetime(2026, 1, 1, tzinfo=UTC),
    )

    signal = strategy.get_exit_signal(position, current_price=100.0)

    assert signal is not None
    assert signal.action == "sell"
    assert signal.metadata["exit_reason"] in {"trailing_stop", "atr_stop"}


def test_factor_strategy_respects_quarterly_rebalance_and_ranking(tmp_path) -> None:
    settings = build_settings(tmp_path)
    provider = FakeStrategyDataProvider(
        factors={
            "AAA": FactorSnapshot("AAA", "KR", 0.9, 0.8, 0.7, 0.6),
            "BBB": FactorSnapshot("BBB", "KR", 0.8, 0.8, 0.8, 0.8),
            "CCC": FactorSnapshot("CCC", "KR", 0.1, 0.1, 0.1, 0.1),
        }
    )
    config = settings.strategies.factor_investing.model_copy(update={"top_n": 2})
    strategy = FactorInvestingStrategy(config, data_provider=provider)

    signals = strategy.generate_signals(["AAA", "BBB", "CCC"], "KR", datetime(2026, 4, 1, tzinfo=UTC))

    assert [(signal.ticker, signal.action) for signal in signals] == [
        ("AAA", "buy"),
        ("BBB", "buy"),
        ("CCC", "sell"),
    ]
