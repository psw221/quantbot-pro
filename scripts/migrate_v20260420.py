from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.settings import Settings, get_settings
from data.database import init_engine


def migrate(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    engine = init_engine(settings)
    with engine.begin() as connection:
        column_names = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(orders)"))
        }
        if "kis_order_orgno" not in column_names:
            connection.execute(text("ALTER TABLE orders ADD COLUMN kis_order_orgno TEXT"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_orders_kis_order_orgno ON orders (kis_order_orgno)"))


def main() -> None:
    settings = get_settings()
    print(f"Target database: {settings.database.absolute_path}")
    migrate(settings)
    print("Applied migration: add orders.kis_order_orgno and idx_orders_kis_order_orgno")


if __name__ == "__main__":
    main()
