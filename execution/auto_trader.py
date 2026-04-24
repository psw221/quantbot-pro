from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import sqrt
from typing import Any

from core.models import EventFlag, PositionSnapshot, RiskDecision, Signal, SizingDecision, SizingInput
from core.settings import Settings, get_settings
from data.database import Order, PortfolioSnapshot, Position, get_read_session
from execution.market_constraints import MarketConstraintInput, MarketConstraintValidator
from execution.order_manager import OrderManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from strategy.base import BaseStrategy, StrategyDataProvider, StrategyInputAvailability
from strategy.dual_momentum import DualMomentumStrategy
from strategy.factor_investing import FactorInvestingStrategy
from strategy.signal_resolver import SignalResolver
from strategy.trend_following import TrendFollowingStrategy


ACTIVE_ORDER_STATUSES = {
    "pending",
    "validated",
    "submitted",
    "partially_filled",
    "cancel_pending",
    "reconcile_hold",
}
PRICE_CONTEXT_LOOKBACK_DAYS = 30

UniverseLoader = Callable[[str, datetime], list[str]]
StrategyBuilder = Callable[[Settings, StrategyDataProvider], BaseStrategy]
CashAvailableLoader = Callable[[str, datetime], float]


def _default_strategy_builders() -> dict[str, StrategyBuilder]:
    return {
        "dual_momentum": lambda settings, provider: DualMomentumStrategy(
            settings.strategies.dual_momentum,
            data_provider=provider,
        ),
        "factor_investing": lambda settings, provider: FactorInvestingStrategy(
            settings.strategies.factor_investing,
            data_provider=provider,
        ),
        "trend_following": lambda settings, provider: TrendFollowingStrategy(
            settings.strategies.trend_following,
            data_provider=provider,
        ),
    }


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _estimate_annualized_volatility(closes: list[float]) -> float:
    if len(closes) < 2:
        return 0.0
    returns = []
    for previous, current in zip(closes, closes[1:]):
        if previous <= 0:
            continue
        returns.append((current - previous) / previous)
    if len(returns) < 2:
        return 0.0
    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / len(returns)
    return sqrt(variance) * sqrt(252)


@dataclass(slots=True)
class AutoTradeSignalRejection:
    signal: Signal
    reason: str
    detail: str | None = None


@dataclass(slots=True)
class ResolvedOrderCandidate:
    signal: Signal
    quantity: int
    current_price: float
    order_type: str
    price: float | None
    position: PositionSnapshot | None
    risk_decision: RiskDecision
    sizing_decision: SizingDecision
    event_flags: list[EventFlag] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AutoTradeCycleResult:
    market: str
    as_of: datetime
    source_env: str
    universe: list[str]
    cash_available: float
    configured_strategies: list[str]
    generated_signals: list[Signal] = field(default_factory=list)
    resolved_signals: list[Signal] = field(default_factory=list)
    order_candidates: list[ResolvedOrderCandidate] = field(default_factory=list)
    rejected_signals: list[AutoTradeSignalRejection] = field(default_factory=list)
    submitted_orders: list[str] = field(default_factory=list)
    strategy_diagnostics: list["StrategyCycleDiagnostic"] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def signals_generated(self) -> int:
        return len(self.generated_signals)

    @property
    def signals_resolved(self) -> int:
        return len(self.resolved_signals)

    @property
    def orders_submitted(self) -> int:
        return len(self.submitted_orders)


@dataclass(slots=True)
class StrategyCycleDiagnostic:
    strategy_name: str
    status: str
    skip_reason: str | None = None
    factor_input_available: bool | None = None


class AutoTrader:
    def __init__(
        self,
        *,
        data_provider: StrategyDataProvider,
        universe_loader: UniverseLoader,
        signal_resolver: SignalResolver | None = None,
        risk_manager: RiskManager | None = None,
        position_sizer: PositionSizer | None = None,
        order_manager: OrderManager | None = None,
        market_constraint_validator: MarketConstraintValidator | None = None,
        cash_available_loader: CashAvailableLoader | None = None,
        read_session_factory: Callable[[], Any] | None = None,
        strategy_builders: Mapping[str, StrategyBuilder] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.data_provider = data_provider
        self.universe_loader = universe_loader
        self.signal_resolver = signal_resolver or SignalResolver()
        self.risk_manager = risk_manager or RiskManager(self.settings)
        self.position_sizer = position_sizer or PositionSizer(self.settings)
        self.order_manager = order_manager
        self.market_constraint_validator = market_constraint_validator or MarketConstraintValidator(self.settings)
        self.cash_available_loader = cash_available_loader
        self.read_session_factory = read_session_factory or get_read_session
        self.strategy_builders = dict(strategy_builders or _default_strategy_builders())

    def run_cycle(
        self,
        market: str,
        as_of: datetime,
        *,
        strategies: list[str] | None = None,
    ) -> AutoTradeCycleResult:
        normalized_market = market.upper()
        as_of_utc = _coerce_utc(as_of)
        selected_strategies = self._resolve_cycle_strategies(strategies)
        universe = _dedupe_preserve_order(self.universe_loader(normalized_market, as_of_utc))
        positions = self._load_positions(normalized_market, strategy_names=selected_strategies)
        position_tickers = [position.ticker for position in positions.values()]
        tickers_for_context = _dedupe_preserve_order(universe + position_tickers)
        cash_available = self._load_cash_available(normalized_market, as_of_utc)
        event_flags = self.data_provider.get_event_flags(tickers_for_context, normalized_market, as_of_utc) if tickers_for_context else []
        strategy_instances = self._build_strategy_instances(selected_strategies)

        generated_signals: list[Signal] = []
        strategy_diagnostics: list[StrategyCycleDiagnostic] = []
        if universe:
            for strategy_name in selected_strategies:
                diagnostic = self._evaluate_strategy_diagnostic(
                    strategy_name=strategy_name,
                    market=normalized_market,
                    as_of=as_of_utc,
                )
                if diagnostic.status == "skipped":
                    strategy_diagnostics.append(diagnostic)
                    continue
                strategy = strategy_instances[strategy_name]
                generated_signals.extend(strategy.generate_signals(universe, normalized_market, as_of_utc))
                strategy_diagnostics.append(diagnostic)
        else:
            for strategy_name in selected_strategies:
                strategy_diagnostics.append(
                    self._evaluate_strategy_diagnostic(
                        strategy_name=strategy_name,
                        market=normalized_market,
                        as_of=as_of_utc,
                    )
                )
        skipped_strategies = {item.strategy_name for item in strategy_diagnostics if item.status == "skipped"}

        latest_prices, previous_closes, volatilities = self._load_price_context(tickers_for_context, normalized_market, as_of_utc)
        exit_signals, exit_rejections = self._build_exit_signals(
            positions=positions,
            latest_prices=latest_prices,
            strategies=strategy_instances,
            skipped_strategy_names=skipped_strategies,
        )
        resolved_signals = self.signal_resolver.resolve(generated_signals + exit_signals)
        open_order_tickers = self._load_open_order_tickers(normalized_market)

        result = AutoTradeCycleResult(
            market=normalized_market,
            as_of=as_of_utc,
            source_env=self.settings.env.value,
            universe=universe,
            cash_available=cash_available,
            configured_strategies=list(selected_strategies),
            generated_signals=generated_signals + exit_signals,
            resolved_signals=resolved_signals,
            rejected_signals=exit_rejections,
            strategy_diagnostics=strategy_diagnostics,
            details={
                "position_tickers": sorted(position_tickers),
                "open_order_tickers": sorted(open_order_tickers),
                "max_orders_per_cycle": self.settings.auto_trading.max_orders_per_cycle,
            },
        )

        for signal in resolved_signals:
            position = positions.get((signal.ticker, signal.strategy))
            applicable_event_flags = [flag for flag in event_flags if flag.ticker is None or flag.ticker == signal.ticker]

            if signal.ticker in open_order_tickers:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason="open_order_exists",
                    )
                )
                continue

            if signal.action == "buy" and position is not None and position.quantity > 0:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason="existing_position_reentry_blocked",
                    )
                )
                continue

            current_price = latest_prices.get(signal.ticker)
            if current_price is None or current_price <= 0:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason="data_unavailable",
                        detail="latest_price_missing",
                    )
                )
                continue

            if signal.action == "sell" and (position is None or position.quantity <= 0):
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason="no_position_to_sell",
                    )
                )
                continue

            risk_decision = self.risk_manager.evaluate_signal(
                signal,
                current_price=current_price,
                position=position,
                blocked=False,
                event_flags=applicable_event_flags,
            )
            if not risk_decision.approved:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason=risk_decision.reason,
                    )
                )
                continue

            if signal.action == "sell":
                assert position is not None
                sizing_decision = SizingDecision(
                    quantity=position.quantity,
                    target_notional=position.quantity * current_price,
                    capped=False,
                    reason="exit_full_position",
                    volatility_scale=1.0,
                )
            elif signal.action == "buy":
                sizing_decision = self.position_sizer.size_position(
                    SizingInput(
                        ticker=signal.ticker,
                        market=normalized_market,
                        strategy=signal.strategy,
                        cash_available=cash_available,
                        price=current_price,
                        volatility=volatilities.get(signal.ticker, 0.0),
                        target_volatility=(
                            self.settings.strategies.trend_following.target_volatility
                            if signal.strategy == "trend_following"
                            else None
                        ),
                        min_position_fraction=self.settings.strategies.min_position_fraction,
                        risk_scale=risk_decision.scale_factor,
                    )
                )
                if sizing_decision.quantity <= 0:
                    result.rejected_signals.append(
                        AutoTradeSignalRejection(
                            signal=signal,
                            reason=sizing_decision.reason,
                        )
                    )
                    continue
            else:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason="unsupported_action",
                    )
                )
                continue

            constraint_decision = self.market_constraint_validator.evaluate(
                MarketConstraintInput(
                    signal=signal,
                    quantity=sizing_decision.quantity,
                    current_price=current_price,
                    previous_close=previous_closes.get(signal.ticker),
                    as_of=as_of_utc,
                    position=position,
                    order_type="market",
                    price=None,
                    cash_available=cash_available,
                    event_flags=applicable_event_flags,
                )
            )
            if not constraint_decision.approved:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=signal,
                        reason=constraint_decision.reason,
                    )
                )
                continue

            result.order_candidates.append(
                ResolvedOrderCandidate(
                    signal=signal,
                    quantity=sizing_decision.quantity,
                    current_price=current_price,
                    order_type="market",
                    price=None,
                    position=position,
                    risk_decision=risk_decision,
                    sizing_decision=sizing_decision,
                    event_flags=applicable_event_flags,
                    metadata={
                        "source_strategies": signal.metadata.get("source_strategies", [signal.strategy]),
                        "risk_scale": risk_decision.scale_factor,
                    },
                )
            )

        return result

    def execute_cycle(
        self,
        market: str,
        as_of: datetime,
        *,
        access_token: str | None = None,
        strategies: list[str] | None = None,
    ) -> AutoTradeCycleResult:
        if self.order_manager is None:
            raise ValueError("order_manager is required for execute_cycle")

        result = self.run_cycle(market, as_of, strategies=strategies)
        submitted_order_count = 0
        submitted_notional = 0.0
        max_orders = self.settings.auto_trading.max_orders_per_cycle
        max_notional = self.settings.auto_trading.max_order_notional_per_cycle

        for candidate in result.order_candidates:
            if self.order_manager.trading_blocked:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=candidate.signal,
                        reason="trading_blocked",
                    )
                )
                continue

            candidate_notional = candidate.quantity * candidate.current_price
            if submitted_order_count >= max_orders:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=candidate.signal,
                        reason="cycle_order_limit",
                    )
                )
                continue
            if submitted_notional + candidate_notional > max_notional:
                result.rejected_signals.append(
                    AutoTradeSignalRejection(
                        signal=candidate.signal,
                        reason="cycle_notional_limit",
                    )
                )
                continue

            signal_id = self.order_manager.persist_signal(candidate.signal)
            intent = self.order_manager.create_order_intent(
                candidate.signal,
                signal_id=signal_id,
                quantity=candidate.quantity,
                order_type=candidate.order_type,
                price=candidate.price,
                risk_decision=candidate.risk_decision,
            )
            submission = self.order_manager.persist_validated_order(intent)
            self.order_manager.place_order(
                submission.order_id,
                self._build_broker_payload(candidate),
                access_token=access_token,
            )

            submitted_order = self._load_order(submission.order_id)
            if submitted_order is not None and submitted_order.status == "submitted":
                result.submitted_orders.append(submission.client_order_id)
                submitted_order_count += 1
                submitted_notional += candidate_notional
                continue

            rejection_reason = (
                submitted_order.error_message
                if submitted_order is not None and submitted_order.error_message
                else "order_submit_failed"
            )
            result.rejected_signals.append(
                AutoTradeSignalRejection(
                    signal=candidate.signal,
                    reason=rejection_reason,
                    detail=submitted_order.status if submitted_order is not None else None,
                )
            )

        result.details["submitted_order_count"] = submitted_order_count
        result.details["submitted_notional_krw"] = submitted_notional
        return result

    def _evaluate_strategy_diagnostic(
        self,
        *,
        strategy_name: str,
        market: str,
        as_of: datetime,
    ) -> StrategyCycleDiagnostic:
        if strategy_name != "factor_investing":
            return StrategyCycleDiagnostic(strategy_name=strategy_name, status="completed")

        availability = self._describe_factor_input_availability(market, as_of)
        if not availability.available:
            return StrategyCycleDiagnostic(
                strategy_name=strategy_name,
                status="skipped",
                skip_reason=availability.reason or "factor_input_unavailable",
                factor_input_available=False,
            )
        return StrategyCycleDiagnostic(
            strategy_name=strategy_name,
            status="completed",
            factor_input_available=True,
        )

    def _describe_factor_input_availability(self, market: str, as_of: datetime) -> StrategyInputAvailability:
        describe_availability = getattr(self.data_provider, "describe_factor_input_availability", None)
        if callable(describe_availability):
            availability = describe_availability(market, as_of)
            if isinstance(availability, StrategyInputAvailability):
                return availability
        return StrategyInputAvailability(available=True)

    def _resolve_cycle_strategies(self, strategies: list[str] | None) -> list[str]:
        if strategies is None:
            return list(self.settings.auto_trading.strategies)

        selected_strategies = _dedupe_preserve_order(list(strategies))
        if not selected_strategies:
            raise ValueError("auto-trading strategy subset must not be empty")

        configured_strategies = set(self.settings.auto_trading.strategies)
        invalid_strategies = [name for name in selected_strategies if name not in configured_strategies]
        if invalid_strategies:
            joined = ", ".join(invalid_strategies)
            raise ValueError(f"auto-trading strategy subset must be enabled in settings: {joined}")

        return selected_strategies

    def _build_strategy_instances(self, strategy_names: list[str]) -> dict[str, BaseStrategy]:
        instances: dict[str, BaseStrategy] = {}
        for strategy_name in strategy_names:
            builder = self.strategy_builders.get(strategy_name)
            if builder is None:
                raise ValueError(f"unsupported auto-trading strategy: {strategy_name}")
            instances[strategy_name] = builder(self.settings, self.data_provider)
        return instances

    def _load_positions(
        self,
        market: str,
        *,
        strategy_names: list[str] | None = None,
    ) -> dict[tuple[str, str], PositionSnapshot]:
        with self.read_session_factory() as session:
            query = session.query(Position).filter(
                Position.market == market,
                Position.quantity > 0,
            )
            if strategy_names is not None:
                query = query.filter(Position.strategy.in_(strategy_names))
            rows = list(query.all())

        positions: dict[tuple[str, str], PositionSnapshot] = {}
        for row in rows:
            positions[(row.ticker, row.strategy)] = PositionSnapshot(
                ticker=row.ticker,
                market=row.market,
                strategy=row.strategy,  # type: ignore[arg-type]
                quantity=row.quantity,
                avg_cost=row.avg_cost,
                current_price=row.current_price,
                highest_price=row.highest_price,
                entry_date=row.entry_date,
            )
        return positions

    def _load_open_order_tickers(self, market: str) -> set[str]:
        with self.read_session_factory() as session:
            rows = list(
                session.query(Order.ticker)
                .filter(
                    Order.market == market,
                    Order.status.in_(ACTIVE_ORDER_STATUSES),
                )
                .distinct()
                .all()
            )
        return {row[0] for row in rows}

    def _load_order(self, order_id: int) -> Order | None:
        with self.read_session_factory() as session:
            return session.get(Order, order_id)

    def _load_cash_available(self, market: str, as_of: datetime) -> float:
        with self.read_session_factory() as session:
            row = session.query(PortfolioSnapshot).order_by(PortfolioSnapshot.snapshot_date.desc()).first()
        if row is not None:
            return float(row.cash_krw)
        if self.cash_available_loader is None:
            return 0.0
        try:
            return float(self.cash_available_loader(market, as_of))
        except Exception:
            return 0.0

    @staticmethod
    def _build_broker_payload(candidate: ResolvedOrderCandidate) -> dict[str, Any]:
        return {
            "ticker": candidate.signal.ticker,
            "market": candidate.signal.market,
            "side": "buy" if candidate.signal.action == "buy" else "sell",
            "quantity": candidate.quantity,
            "order_type": candidate.order_type,
            "price": candidate.price,
        }

    def _load_price_context(
        self,
        tickers: list[str],
        market: str,
        as_of: datetime,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        if not tickers:
            return {}, {}, {}
        histories = self.data_provider.get_price_history(
            tickers,
            market,  # type: ignore[arg-type]
            as_of,
            PRICE_CONTEXT_LOOKBACK_DAYS,
        )
        latest_prices: dict[str, float] = {}
        previous_closes: dict[str, float] = {}
        volatilities: dict[str, float] = {}
        for ticker, bars in histories.items():
            if not bars:
                continue
            sorted_bars = sorted(bars, key=lambda bar: _coerce_utc(bar.timestamp))
            closes = [float(bar.close) for bar in sorted_bars if bar.close > 0]
            if not closes:
                continue
            latest_prices[ticker] = closes[-1]
            if len(closes) >= 2:
                previous_closes[ticker] = closes[-2]
            volatilities[ticker] = _estimate_annualized_volatility(closes)
        return latest_prices, previous_closes, volatilities

    def _build_exit_signals(
        self,
        *,
        positions: dict[tuple[str, str], PositionSnapshot],
        latest_prices: dict[str, float],
        strategies: dict[str, BaseStrategy],
        skipped_strategy_names: set[str],
    ) -> tuple[list[Signal], list[AutoTradeSignalRejection]]:
        signals: list[Signal] = []
        rejections: list[AutoTradeSignalRejection] = []
        for position in positions.values():
            if position.strategy in skipped_strategy_names:
                continue
            strategy = strategies.get(position.strategy)
            if strategy is None:
                continue

            current_price = latest_prices.get(position.ticker)
            if current_price is None or current_price <= 0:
                if position.current_price <= 0:
                    rejections.append(
                        AutoTradeSignalRejection(
                            signal=Signal(
                                ticker=position.ticker,
                                market=position.market,
                                action="sell",
                                strategy=position.strategy,
                                strength=1.0,
                                reason="exit_evaluation_data_unavailable",
                                is_exit=True,
                                signal_source="auto_trader",
                            ),
                            reason="data_unavailable",
                            detail="position_price_missing",
                        )
                    )
                    continue
                current_price = position.current_price

            exit_signal = strategy.get_exit_signal(position, current_price)
            if exit_signal is not None:
                signals.append(exit_signal)
        return signals, rejections
