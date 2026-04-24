from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.settings import get_settings


def main() -> int:
    settings = get_settings()
    print(
        "config ok: "
        f"env={settings.env.value}, "
        f"db={settings.database.path}, "
        f"auto_trading_enabled={settings.auto_trading.enabled}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
