from auth.token_manager import TokenManager
from core.settings import get_settings
from data.database import init_db
from execution.kis_api import KISApiClient
from execution.order_manager import OrderManager
from execution.reconciliation import ReconciliationService
from execution.runtime import TradingRuntime
from execution.writer_queue import WriterQueue


def main() -> None:
    settings = get_settings()
    init_db(settings)

    writer_queue = WriterQueue.from_settings(settings)
    token_manager = None
    api_client = None
    order_manager = None
    reconciliation_service = None
    if settings.kis.credentials is not None:
        api_client = KISApiClient(settings=settings)
        token_manager = TokenManager(writer_queue=writer_queue, api_client=api_client, settings=settings)
        order_manager = OrderManager(writer_queue=writer_queue, api_client=api_client, settings=settings)
        reconciliation_service = ReconciliationService(writer_queue=writer_queue, settings=settings)

    runtime = TradingRuntime(
        writer_queue=writer_queue,
        token_manager=token_manager,
        api_client=api_client,
        order_manager=order_manager,
        reconciliation_service=reconciliation_service,
        settings=settings,
    )
    print(
        f"QuantBot Pro runtime ready: env={settings.env.value}, "
        f"db={settings.database.path}"
    )
    runtime.run_forever()


if __name__ == "__main__":
    main()
