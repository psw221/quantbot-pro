from auth.token_manager import TokenManager
from core.settings import get_settings
from data.collector import (
    build_composite_kr_price_history_loader,
    build_default_kr_universe_loader,
    build_kis_kr_price_history_loader,
    build_pykrx_price_history_loader,
)
from data.database import init_db
from execution.auto_trader import AutoTrader
from execution.kis_api import KISApiClient
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.runtime import TradingRuntime
from execution.writer_queue import WriterQueue
from monitor.operations import OperationsRecorder
from monitor.telegram_bot import TelegramNotifier
from strategy.data_provider import FactorInputLoader, KRStrategyDataProvider


def build_strategy_cycle_runner(
    *,
    settings,
    token_manager: TokenManager | None,
    api_client: KISApiClient | None,
    order_manager: OrderManager | None,
    factor_input_loader: FactorInputLoader | None = None,
    auto_trader: AutoTrader | None = None,
):
    if not settings.auto_trading.enabled:
        return None
    if token_manager is None or order_manager is None or api_client is None:
        return None

    cycle_access_token: str | None = None

    def current_cycle_access_token() -> str:
        return cycle_access_token or token_manager.get_valid_token(settings.env)

    price_history_loader = build_composite_kr_price_history_loader(
        build_pykrx_price_history_loader(),
        build_kis_kr_price_history_loader(
            api_client=api_client,
            token_manager=token_manager,
            env=settings.env,
            access_token_provider=current_cycle_access_token,
        ),
    )

    def load_cash_available(market: str, as_of):
        if market.upper() != "KR":
            return 0.0
        access_token = current_cycle_access_token()
        return api_client.normalize_cash_available(api_client.get_cash_balance(access_token))

    trader = auto_trader or AutoTrader(
        data_provider=KRStrategyDataProvider(
            price_history_loader=price_history_loader,
            factor_input_loader=factor_input_loader,
            settings=settings,
        ),
        universe_loader=build_default_kr_universe_loader(),
        order_manager=order_manager,
        cash_available_loader=load_cash_available,
        settings=settings,
    )

    def run_cycle(market: str, as_of):
        nonlocal cycle_access_token
        cycle_access_token = token_manager.get_valid_token(settings.env)
        try:
            return trader.execute_cycle(market, as_of, access_token=cycle_access_token)
        finally:
            cycle_access_token = None

    return run_cycle


def main() -> None:
    settings = get_settings()
    init_db(settings)

    writer_queue = WriterQueue.from_settings(settings)
    token_manager = None
    api_client = None
    order_manager = None
    reconciliation_service = None
    operations_recorder = OperationsRecorder(writer_queue)
    telegram_notifier = TelegramNotifier(settings=settings)
    if settings.kis.credentials is not None:
        api_client = KISApiClient(settings=settings)
        token_manager = TokenManager(writer_queue=writer_queue, api_client=api_client, settings=settings)
        order_manager = OrderManager(
            writer_queue=writer_queue,
            api_client=api_client,
            telegram_notifier=telegram_notifier,
            settings=settings,
        )
        reconciliation_service = ReconciliationService(writer_queue=writer_queue, settings=settings)

    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=token_manager,
        api_client=api_client,
        order_manager=order_manager,
        reconciliation_service=reconciliation_service,
        operations_recorder=operations_recorder,
        telegram_notifier=telegram_notifier,
        settings=settings,
        strategy_cycle_runner=build_strategy_cycle_runner(
            settings=settings,
            token_manager=token_manager,
            api_client=api_client,
            order_manager=order_manager,
        ),
    )
    print(
        f"QuantBot Pro runtime ready: env={settings.env.value}, "
        f"db={settings.database.path}"
    )
    runtime.run_forever()


if __name__ == "__main__":
    main()
