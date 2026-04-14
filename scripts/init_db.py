from core.settings import get_settings
from data.database import init_db


def main() -> None:
    settings = get_settings()
    init_db(settings)
    print(f"Initialized database at {settings.database.absolute_path}")


if __name__ == "__main__":
    main()
