from core.settings import get_settings
from data.database import init_db
from execution.writer_queue import WriterQueue


def main() -> None:
    settings = get_settings()
    init_db(settings)

    writer_queue = WriterQueue.from_settings(settings)
    writer_queue.start()
    try:
        print(
            f"QuantBot Pro bootstrap ready: env={settings.env.value}, "
            f"db={settings.database.path}"
        )
    finally:
        writer_queue.stop()


if __name__ == "__main__":
    main()
