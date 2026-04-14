from __future__ import annotations

from collections import defaultdict

from core.models import Signal


class SignalResolver:
    def resolve(self, signals: list[Signal]) -> list[Signal]:
        grouped: dict[tuple[str, str], list[Signal]] = defaultdict(list)
        for signal in signals:
            grouped[(signal.ticker, signal.market)].append(signal)

        resolved: list[Signal] = []
        for _, bucket in grouped.items():
            sells = [signal for signal in bucket if signal.action == "sell"]
            buys = [signal for signal in bucket if signal.action == "buy"]

            if sells:
                strongest_sell = max(sells, key=lambda signal: signal.strength)
                strongest_sell.metadata.setdefault("resolver_notes", []).append("sell_priority")
                resolved.append(strongest_sell)
                continue

            if buys:
                merged = buys[0]
                if len(buys) > 1:
                    merged.strength = sum(signal.strength for signal in buys)
                    merged.reason = "; ".join(signal.reason for signal in buys)
                    merged.metadata.setdefault("resolver_notes", []).append("merged_multi_buy")
                    merged.metadata["source_strategies"] = [signal.strategy for signal in buys]
                resolved.append(merged)

        return resolved
