from core.settings import get_settings
from data.database import init_db


def main() -> None:
    settings = get_settings()
    init_db(settings)
    print(f"Initialized database at {settings.database.absolute_path}")
    print("Schema initialization is idempotent.")
    print("For schema changes after deployment, add and run a dedicated scripts/migrate_vYYYYMMDD.py migration.")


if __name__ == "__main__":
    main()
