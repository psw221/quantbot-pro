from core import settings as settings_module


def pytest_runtest_setup() -> None:
    settings_module.get_settings.cache_clear()


def pytest_runtest_teardown() -> None:
    settings_module.get_settings.cache_clear()
