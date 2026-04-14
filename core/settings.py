from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, model_validator

from core.exceptions import ConfigurationError


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"
ENV_PATH = ROOT_DIR / "config" / ".env"


class RuntimeEnv(str, Enum):
    VTS = "vts"
    PROD = "prod"


class DatabaseSettings(BaseModel):
    path: str = "data/quantbot.db"
    busy_timeout_ms: int = 5000

    @property
    def absolute_path(self) -> Path:
        return (ROOT_DIR / self.path).resolve()


class LoggingSettings(BaseModel):
    level: str = "INFO"
    directory: str = "logs"

    @property
    def absolute_directory(self) -> Path:
        return (ROOT_DIR / self.directory).resolve()


class KISEndpointSettings(BaseModel):
    rest_base_url: str
    websocket_base_url: str
    token_path: str = "/oauth2/tokenP"


class KISCredentials(BaseModel):
    app_key: SecretStr
    app_secret: SecretStr
    account_no: str | None = None
    product_code: str | None = None


class KISSettings(BaseModel):
    app_key_env: str = "KIS_APP_KEY"
    app_secret_env: str = "KIS_APP_SECRET"
    account_no_env: str = "KIS_ACCOUNT_NO"
    product_code_env: str = "KIS_PRODUCT_CODE"
    token_refresh_hour_kst: int = 8
    rate_limit_per_sec: int = 20
    request_timeout_sec: int = 10
    environments: dict[RuntimeEnv, KISEndpointSettings]
    credentials: KISCredentials | None = None

    def endpoint_for(self, env: RuntimeEnv) -> KISEndpointSettings:
        return self.environments[env]


class RebalancingSettings(BaseModel):
    macro_threshold_pct_point: float = 0.05
    macro_check: str = "monthly_eom"
    broker_poll_interval_min: int = 10


class RiskSettings(BaseModel):
    max_single_stock_domestic: float = 0.05
    max_single_stock_overseas: float = 0.03
    max_sector_weight: float = 0.25
    stop_loss_domestic: float = -0.07
    stop_loss_overseas: float = -0.05
    trailing_stop: float = -0.10
    daily_max_loss: float = -0.02
    max_drawdown_limit: float = -0.15


class AllocationSettings(BaseModel):
    domestic: float = 0.60
    overseas: float = 0.30
    cash_buffer: float = 0.10

    @model_validator(mode="after")
    def validate_total(self) -> "AllocationSettings":
        total = self.domestic + self.overseas + self.cash_buffer
        if round(total, 6) != 1.0:
            raise ConfigurationError("allocation weights must sum to 1.0")
        return self


class StrategyWeightsSettings(BaseModel):
    dual_momentum: float = 0.30
    trend_following: float = 0.25
    factor_investing: float = 0.45

    @model_validator(mode="after")
    def validate_total(self) -> "StrategyWeightsSettings":
        total = self.dual_momentum + self.trend_following + self.factor_investing
        if round(total, 6) != 1.0:
            raise ConfigurationError("strategy_weights must sum to 1.0")
        return self


class Settings(BaseModel):
    env: RuntimeEnv = RuntimeEnv.VTS
    allocation: AllocationSettings = Field(default_factory=AllocationSettings)
    strategy_weights: StrategyWeightsSettings = Field(default_factory=StrategyWeightsSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    kis: KISSettings
    rebalancing: RebalancingSettings = Field(default_factory=RebalancingSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)

    @model_validator(mode="after")
    def validate_poll_interval(self) -> "Settings":
        if self.rebalancing.broker_poll_interval_min < 10:
            raise ConfigurationError("broker_poll_interval_min must be at least 10 minutes")
        return self


def _load_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigurationError("config.yaml must contain a mapping at the top level")
    return data


def _load_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _resolve_kis_credentials(kis_config: dict[str, Any], env_values: dict[str, str]) -> dict[str, Any]:
    app_key_name = kis_config.get("app_key_env", "KIS_APP_KEY")
    app_secret_name = kis_config.get("app_secret_env", "KIS_APP_SECRET")
    account_no_name = kis_config.get("account_no_env", "KIS_ACCOUNT_NO")
    product_code_name = kis_config.get("product_code_env", "KIS_PRODUCT_CODE")

    app_key = env_values.get(app_key_name, "").strip()
    app_secret = env_values.get(app_secret_name, "").strip()
    if not app_key or not app_secret:
        return {**kis_config, "credentials": None}

    return {
        **kis_config,
        "credentials": {
            "app_key": app_key,
            "app_secret": app_secret,
            "account_no": env_values.get(account_no_name),
            "product_code": env_values.get(product_code_name),
        },
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_data = _load_yaml_config(CONFIG_PATH)
    env_values = _load_env_values(ENV_PATH)

    kis_config = config_data.get("kis")
    if not isinstance(kis_config, dict):
        raise ConfigurationError("config.yaml is missing a 'kis' section")

    config_data = {
        **config_data,
        "kis": _resolve_kis_credentials(kis_config, env_values),
    }
    return Settings.model_validate(config_data)
