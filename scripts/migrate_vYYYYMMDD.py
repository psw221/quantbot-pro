from __future__ import annotations

from core.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"Target database: {settings.database.absolute_path}")
    print("Migration placeholder script.")
    print("Workflow: update docs/DB_SCHEMA_v1.2.md -> update ORM -> implement this migration -> validate in VTS.")
    print("Do not modify schema directly in production without a dedicated migration script.")


if __name__ == "__main__":
    main()
