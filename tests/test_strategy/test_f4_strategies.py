from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.models import FactorSnapshot, IntradayBar, PositionSnapshot, PriceBar
from strategy.dual_momentum import DualMomentumStrategy
from strategy.factor_investing import FactorInvestingStrategy
from strategy.intraday_momentum import IntradayMomentumStrategy
from strategy.trend_following import TrendFollowingStrategy
from tests.test_execution.test_bootstrap import build_settings


class FakeStrategyDataProvider:
    def __init__(
        self,
        *,
        prices: dict[str, list[PriceBar]] | None = None,
        intraday: dict[str, list[IntradayBar]] | None = None,
        factors: dict[str, FactorSnapshot] | None = None,
    ) -> None:
        self.prices = prices or {}
        self.intraday = intraday or {}
        self.factors = factors or {}

    def get_price_history(self, tickers, market, as_of, lookback_days):
        return {ticker: self.prices.get(ticker, []) for ticker in tickers}

    def get_intraday_bars(self, tickers, market, as_of, lookback_minutes):
        return {ticker: self.intraday.get(ticker, []) for ticker in tickers}

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


def _intraday_bars(
    ticker: str,
    *,
    opening_close: float = 100.0,
    latest_close: float = 103.0,
    latest_volume: int = 200,
    extra_bars: list[IntradayBar] | None = None,
) -> list[IntradayBar]:
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    bars = [
        IntradayBar(
            ticker=ticker,
            market="KR",
            timestamp=start + timedelta(minutes=index),
            open=opening_close,
            high=opening_close + 1,
            low=opening_close - 1,
            close=opening_close,
            volume=100,
        )
        for index in range(30)
    ]
    if extra_bars:
        bars.extend(extra_bars)
    bars.append(
        IntradayBar(
            ticker=ticker,
            market="KR",
            timestamp=start + timedelta(minutes=35),
            open=latest_close - 1,
            high=latest_close + 1,
            low=latest_close - 1,
            close=latest_close,
            volume=latest_volume,
        )
    )
    return sorted(bars, key=lambda bar: bar.timestamp)


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


def test_intraday_momentum_generates_opening_range_vwap_breakout_buy(tmp_path) -> None:
    settings = build_settings(tmp_path)
    provider = FakeStrategyDataProvider(intraday={"005930": _intraday_bars("005930")})
    strategy = IntradayMomentumStrategy(settings.strategies.intraday_momentum, data_provider=provider)

    signals = strategy.generate_signals(["005930"], "KR", datetime(2026, 5, 1, 0, 35, tzinfo=UTC))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.action == "buy"
    assert signal.strategy == "intraday_momentum"
    assert signal.reason == "opening_range_vwap_breakout"
    assert signal.metadata["opening_range_high"] == 101.0
    assert signal.metadata["opening_range_low"] == 99.0
    assert signal.metadata["latest_price"] == 103.0
    assert signal.metadata["volume_ratio"] == 2.0


def test_intraday_momentum_skips_buy_when_price_is_below_vwap(tmp_path) -> None:
    settings = build_settings(tmp_path)
    high_vwap_bar = IntradayBar(
        ticker="005930",
        market="KR",
        timestamp=datetime(2026, 5, 1, 0, 30, tzinfo=UTC),
        open=200,
        high=200,
        low=200,
        close=200,
        volume=10_000,
    )
    provider = FakeStrategyDataProvider(
        intraday={"005930": _intraday_bars("005930", latest_close=102, extra_bars=[high_vwap_bar])}
    )
    strategy = IntradayMomentumStrategy(settings.strategies.intraday_momentum, data_provider=provider)

    signals = strategy.generate_signals(["005930"], "KR", datetime(2026, 5, 1, 0, 35, tzinfo=UTC))

    assert signals == []


def test_intraday_momentum_skips_buy_when_volume_ratio_is_low(tmp_path) -> None:
    settings = build_settings(tmp_path)
    provider = FakeStrategyDataProvider(intraday={"005930": _intraday_bars("005930", latest_volume=50)})
    strategy = IntradayMomentumStrategy(settings.strategies.intraday_momentum, data_provider=provider)

    signals = strategy.generate_signals(["005930"], "KR", datetime(2026, 5, 1, 0, 35, tzinfo=UTC))

    assert signals == []


def test_intraday_momentum_skips_new_entries_outside_entry_window(tmp_path) -> None:
    settings = build_settings(tmp_path)
    provider = FakeStrategyDataProvider(intraday={"005930": _intraday_bars("005930")})
    strategy = IntradayMomentumStrategy(settings.strategies.intraday_momentum, data_provider=provider)

    before_opening_range = strategy.generate_signals(["005930"], "KR", datetime(2026, 5, 1, 0, 29, tzinfo=UTC))
    after_cutoff = strategy.generate_signals(["005930"], "KR", datetime(2026, 5, 1, 6, 10, tzinfo=UTC))

    assert before_opening_range == []
    assert after_cutoff == []


def test_intraday_momentum_skips_ticker_after_daily_entry_limit(tmp_path) -> None:
    settings = build_settings(tmp_path)
    provider = FakeStrategyDataProvider(intraday={"005930": _intraday_bars("005930")})
    strategy = IntradayMomentumStrategy(
        settings.strategies.intraday_momentum,
        data_provider=provider,
        entry_history_loader=lambda ticker, trading_day: 1,
    )

    signals = strategy.generate_signals(["005930"], "KR", datetime(2026, 5, 1, 0, 35, tzinfo=UTC))

    assert signals == []


def test_intraday_momentum_force_exits_after_cutoff(tmp_path) -> None:
    settings = build_settings(tmp_path)
    now = datetime(2026, 5, 1, 6, 15, tzinfo=UTC)
    strategy = IntradayMomentumStrategy(
        settings.strategies.intraday_momentum,
        data_provider=FakeStrategyDataProvider(),
        time_provider=lambda: now,
    )
    position = PositionSnapshot("005930", "KR", "intraday_momentum", 3, 100.0, 103.0, 105.0, now)

    signal = strategy.get_exit_signal(position, current_price=103.0)

    assert signal is not None
    assert signal.action == "sell"
    assert signal.reason == "intraday_force_exit"
    assert signal.is_exit is True


def test_intraday_momentum_exits_on_stop_loss_or_trailing_stop(tmp_path) -> None:
    settings = build_settings(tmp_path)
    now = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)
    strategy = IntradayMomentumStrategy(
        settings.strategies.intraday_momentum,
        data_provider=FakeStrategyDataProvider(),
        time_provider=lambda: now,
    )
    stop_position = PositionSnapshot("005930", "KR", "intraday_momentum", 3, 100.0, 99.0, 101.0, now)
    trailing_position = PositionSnapshot("000660", "KR", "intraday_momentum", 2, 100.0, 100.0, 110.0, now)

    stop_signal = strategy.get_exit_signal(stop_position, current_price=99.0)
    trailing_signal = strategy.get_exit_signal(trailing_position, current_price=109.0)

    assert stop_signal is not None
    assert stop_signal.reason == "intraday_stop_loss"
    assert trailing_signal is not None
    assert trailing_signal.reason == "intraday_trailing_stop"


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
