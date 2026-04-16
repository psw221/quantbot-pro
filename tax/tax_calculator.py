from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import TaxEvent, Trade, get_read_session


@dataclass(slots=True)
class _Lot:
    quantity: int
    remaining_quantity: int
    price: float
    currency: str
    trade_fx_rate: float | None
    settlement_fx_rate: float | None
    fx_rate_source: str | None


class TaxCalculator:
    def __init__(self, session_provider: Callable[[], Any] | None = None) -> None:
        self._session_provider = session_provider or get_read_session

    @contextmanager
    def _session_scope(self) -> Iterator[Session]:
        with self._session_provider() as session:
            yield session

    def calculate_yearly_summary(self, year: int, market: str | None = None) -> dict[str, Any]:
        report_rows = self.build_trade_report(year, market=market)
        by_market: dict[str, dict[str, Any]] = {}

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in report_rows:
            grouped[row["market"]].append(row)

        for market_code, rows in grouped.items():
            by_market[market_code] = {
                "sell_trade_count": len(rows),
                "total_quantity": sum(int(row["quantity"]) for row in rows),
                "realized_gain_loss_krw": round(sum(float(row["realized_gain_loss_krw"]) for row in rows), 4),
                "taxable_gain_krw": round(sum(float(row["taxable_gain_krw"]) for row in rows), 4),
                "total_fees_krw": round(sum(float(row["fee_krw"]) for row in rows), 4),
                "total_taxes_krw": round(sum(float(row["tax_krw"]) for row in rows), 4),
            }

        total_rows = report_rows
        return {
            "year": year,
            "market": market,
            "sell_trade_count": len(total_rows),
            "total_quantity": sum(int(row["quantity"]) for row in total_rows),
            "realized_gain_loss_krw": round(sum(float(row["realized_gain_loss_krw"]) for row in total_rows), 4),
            "taxable_gain_krw": round(sum(float(row["taxable_gain_krw"]) for row in total_rows), 4),
            "total_fees_krw": round(sum(float(row["fee_krw"]) for row in total_rows), 4),
            "total_taxes_krw": round(sum(float(row["tax_krw"]) for row in total_rows), 4),
            "by_market": by_market,
        }

    def build_trade_report(self, year: int, market: str | None = None) -> list[dict[str, Any]]:
        with self._session_scope() as session:
            trades = list(
                session.scalars(
                    select(Trade)
                    .where(Trade.executed_at >= datetime(year, 1, 1), Trade.executed_at < datetime(year + 1, 1, 1))
                    .order_by(Trade.executed_at, Trade.id)
                )
            )
            tax_events = {
                event.trade_id: event
                for event in session.scalars(select(TaxEvent).where(TaxEvent.tax_year == year).order_by(TaxEvent.sell_date, TaxEvent.id))
            }

        if market is not None:
            trades = [trade for trade in trades if trade.market == market]

        lots_by_key: dict[tuple[str, str, str], deque[_Lot]] = defaultdict(deque)
        report_rows: list[dict[str, Any]] = []

        for trade in trades:
            key = (trade.ticker, trade.market, trade.strategy)
            if trade.side == "buy":
                lots_by_key[key].append(
                    _Lot(
                        quantity=trade.quantity,
                        remaining_quantity=trade.quantity,
                        price=trade.price,
                        currency=trade.currency,
                        trade_fx_rate=trade.trade_fx_rate,
                        settlement_fx_rate=trade.settlement_fx_rate,
                        fx_rate_source=trade.fx_rate_source,
                    )
                )
                continue

            if trade.side != "sell":
                continue

            tax_event = tax_events.get(trade.id)
            if tax_event is not None:
                report_rows.append(self._build_tax_event_row(trade, tax_event))
                self._consume_fifo_lots(lots_by_key[key], trade.quantity)
                continue

            report_rows.append(self._build_fifo_row(trade, lots_by_key[key]))

        return report_rows

    def _build_tax_event_row(self, trade: Trade, tax_event: TaxEvent) -> dict[str, Any]:
        sell_fx_rate = self._resolve_fx_rate(tax_event.sell_settlement_fx_rate, tax_event.sell_trade_fx_rate)
        buy_fx_rate = self._resolve_fx_rate(tax_event.buy_settlement_fx_rate, tax_event.buy_trade_fx_rate)

        gross_proceeds_local = trade.amount
        cost_basis_local = tax_event.cost_basis
        gross_proceeds_krw = self._convert_amount(gross_proceeds_local, trade.currency, sell_fx_rate)
        cost_basis_krw = self._convert_amount(cost_basis_local, trade.currency, buy_fx_rate)
        fee_krw = self._convert_amount(trade.fee, trade.currency, sell_fx_rate)
        tax_krw = self._convert_amount(trade.tax, trade.currency, sell_fx_rate)
        realized_gain_loss_krw = gross_proceeds_krw - cost_basis_krw - fee_krw - tax_krw
        realized_gain_loss_local = gross_proceeds_local - cost_basis_local - trade.fee - trade.tax
        taxable_gain_krw = max(gross_proceeds_krw - cost_basis_krw, 0.0)

        return {
            "trade_id": trade.id,
            "ticker": trade.ticker,
            "market": trade.market,
            "strategy": trade.strategy,
            "sell_date": trade.executed_at,
            "quantity": trade.quantity,
            "currency": trade.currency,
            "sell_price": trade.price,
            "gross_proceeds_local": gross_proceeds_local,
            "cost_basis_local": cost_basis_local,
            "realized_gain_loss_local": realized_gain_loss_local,
            "gross_proceeds_krw": gross_proceeds_krw,
            "cost_basis_krw": cost_basis_krw,
            "fee_krw": fee_krw,
            "tax_krw": tax_krw,
            "realized_gain_loss_krw": realized_gain_loss_krw,
            "taxable_gain_krw": taxable_gain_krw,
            "buy_fx_rate": buy_fx_rate,
            "sell_fx_rate": sell_fx_rate,
            "fx_rate_source": tax_event.fx_rate_source or trade.fx_rate_source,
            "source": "tax_event",
        }

    def _build_fifo_row(self, trade: Trade, lots: deque[_Lot]) -> dict[str, Any]:
        remaining = trade.quantity
        cost_basis_local = 0.0
        cost_basis_krw = 0.0
        buy_fx_rate: float | None = None
        fx_rate_source: str | None = None

        while remaining > 0 and lots:
            lot = lots[0]
            consume = min(lot.remaining_quantity, remaining)
            cost_basis_local += consume * lot.price
            lot_fx_rate = self._resolve_fx_rate(lot.settlement_fx_rate, lot.trade_fx_rate)
            buy_fx_rate = lot_fx_rate if buy_fx_rate is None else buy_fx_rate
            fx_rate_source = lot.fx_rate_source if fx_rate_source is None else fx_rate_source
            cost_basis_krw += self._convert_amount(consume * lot.price, lot.currency, lot_fx_rate)
            lot.remaining_quantity -= consume
            remaining -= consume
            if lot.remaining_quantity == 0:
                lots.popleft()

        if remaining > 0:
            raise ValueError(f"insufficient FIFO lots for trade_id={trade.id}")

        sell_fx_rate = self._resolve_fx_rate(trade.settlement_fx_rate, trade.trade_fx_rate)
        gross_proceeds_local = trade.amount
        gross_proceeds_krw = self._convert_amount(gross_proceeds_local, trade.currency, sell_fx_rate)
        fee_krw = self._convert_amount(trade.fee, trade.currency, sell_fx_rate)
        tax_krw = self._convert_amount(trade.tax, trade.currency, sell_fx_rate)
        realized_gain_loss_local = gross_proceeds_local - cost_basis_local - trade.fee - trade.tax
        realized_gain_loss_krw = gross_proceeds_krw - cost_basis_krw - fee_krw - tax_krw
        taxable_gain_krw = max(gross_proceeds_krw - cost_basis_krw, 0.0)

        return {
            "trade_id": trade.id,
            "ticker": trade.ticker,
            "market": trade.market,
            "strategy": trade.strategy,
            "sell_date": trade.executed_at,
            "quantity": trade.quantity,
            "currency": trade.currency,
            "sell_price": trade.price,
            "gross_proceeds_local": gross_proceeds_local,
            "cost_basis_local": cost_basis_local,
            "realized_gain_loss_local": realized_gain_loss_local,
            "gross_proceeds_krw": gross_proceeds_krw,
            "cost_basis_krw": cost_basis_krw,
            "fee_krw": fee_krw,
            "tax_krw": tax_krw,
            "realized_gain_loss_krw": realized_gain_loss_krw,
            "taxable_gain_krw": taxable_gain_krw,
            "buy_fx_rate": buy_fx_rate,
            "sell_fx_rate": sell_fx_rate,
            "fx_rate_source": fx_rate_source or trade.fx_rate_source,
            "source": "fifo_reconstructed",
        }

    def _consume_fifo_lots(self, lots: deque[_Lot], quantity: int) -> None:
        remaining = quantity
        while remaining > 0 and lots:
            lot = lots[0]
            consume = min(lot.remaining_quantity, remaining)
            lot.remaining_quantity -= consume
            remaining -= consume
            if lot.remaining_quantity == 0:
                lots.popleft()
        if remaining > 0:
            raise ValueError("insufficient FIFO lots while consuming tax event trade")

    @staticmethod
    def _resolve_fx_rate(preferred: float | None, fallback: float | None) -> float | None:
        return preferred if preferred not in (None, 0) else fallback

    @staticmethod
    def _convert_amount(amount: float, currency: str, fx_rate: float | None) -> float:
        if currency == "KRW":
            return amount
        if fx_rate is None:
            return amount
        return amount * fx_rate
