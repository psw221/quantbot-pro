from core.models import Signal
from strategy.signal_resolver import SignalResolver


def test_signal_resolver_prioritizes_sell_over_buy() -> None:
    resolver = SignalResolver()
    signals = [
        Signal(ticker="005930", market="KR", action="buy", strategy="dual_momentum", strength=1.0, reason="entry"),
        Signal(ticker="005930", market="KR", action="sell", strategy="trend_following", strength=0.7, reason="exit"),
    ]

    resolved = resolver.resolve(signals)

    assert len(resolved) == 1
    assert resolved[0].action == "sell"


def test_signal_resolver_merges_multiple_buys() -> None:
    resolver = SignalResolver()
    signals = [
        Signal(ticker="AAPL", market="US", action="buy", strategy="dual_momentum", strength=0.4, reason="mom"),
        Signal(ticker="AAPL", market="US", action="buy", strategy="factor_investing", strength=0.6, reason="factor"),
    ]

    resolved = resolver.resolve(signals)

    assert len(resolved) == 1
    assert resolved[0].strength == 1.0
    assert "source_strategies" in resolved[0].metadata
