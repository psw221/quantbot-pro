from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

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
                prioritized_sells = sorted(
                    sells,
                    key=lambda signal: (
                        signal.metadata.get("exit_reason") in {"stop_loss", "atr_stop", "trailing_stop"},
                        signal.strength,
                    ),
                    reverse=True,
                )
                strongest_sell = replace(prioritized_sells[0])
                strongest_sell.metadata.setdefault("resolver_notes", []).append("sell_priority")
                if buys and strongest_sell.metadata.get("exit_reason") in {"stop_loss", "atr_stop", "trailing_stop"}:
                    strongest_sell.metadata["same_day_rebuy_blocked"] = True
                    strongest_sell.metadata["reentry_block_reason"] = strongest_sell.metadata["exit_reason"]
                resolved.append(strongest_sell)
                continue

            if buys:
                merged = replace(buys[0])
                if len(buys) > 1:
                    merged.strength = sum(signal.strength for signal in buys)
                    merged.reason = "; ".join(signal.reason for signal in buys)
                    merged.metadata.setdefault("resolver_notes", []).append("merged_multi_buy")
                    merged.metadata["source_strategies"] = [signal.strategy for signal in buys]
                    merged.metadata["sizing_hints"] = {"proportional_shrink": True}
                resolved.append(merged)

        return resolved
