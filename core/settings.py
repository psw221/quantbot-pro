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


class TelegramCredentials(BaseModel):
    bot_token: SecretStr
    chat_id: str


class TelegramSettings(BaseModel):
    enabled: bool = False
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    request_timeout_sec: int = 5
    credentials: TelegramCredentials | None = None


class MonitorSettings(BaseModel):
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)


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
    kr_price_limit_pct: float = 0.30
    kr_block_auction_entries: bool = True
    kr_opening_auction: str = "08:30-09:00"
    kr_closing_auction: str = "15:20-15:30"
    kr_short_sell_block_enabled: bool = True
    kr_settlement_cash_buffer_pct: float = 0.0

    @model_validator(mode="after")
    def validate_kr_constraints(self) -> "RiskSettings":
        if not 0 < self.kr_price_limit_pct < 1:
            raise ConfigurationError("risk.kr_price_limit_pct must be between 0 and 1")
        if not 0 <= self.kr_settlement_cash_buffer_pct < 1:
            raise ConfigurationError("risk.kr_settlement_cash_buffer_pct must be between 0 and 1")
        _validate_time_range(self.kr_opening_auction, field_name="risk.kr_opening_auction")
        _validate_time_range(self.kr_closing_auction, field_name="risk.kr_closing_auction")
        return self


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


class DualMomentumSettings(BaseModel):
    lookback_days: int = 252
    top_n: int = 10
    rebalance_day_of_month: int = 1
    absolute_momentum_floor: float = 0.0


class TrendFollowingSettings(BaseModel):
    ema_fast_period: int = 20
    ema_slow_period: int = 60
    atr_period: int = 14
    rsi_period: int = 14
    rsi_entry_floor: float = 30.0
    target_volatility: float = 0.13
    atr_stop_multiple: float = 2.0


class FactorInvestingSettings(BaseModel):
    top_n: int = 25
    rebalance_months: list[int] = Field(default_factory=lambda: [1, 4, 7, 10])
    rebalance_day_of_month: int = 1
    value_weight: float = 0.25
    quality_weight: float = 0.25
    momentum_weight: float = 0.25
    low_vol_weight: float = 0.25

    @model_validator(mode="after")
    def validate_weights(self) -> "FactorInvestingSettings":
        total = self.value_weight + self.quality_weight + self.momentum_weight + self.low_vol_weight
        if round(total, 6) != 1.0:
            raise ConfigurationError("factor investing weights must sum to 1.0")
        return self


class StrategySettings(BaseModel):
    dual_momentum: DualMomentumSettings = Field(default_factory=DualMomentumSettings)
    trend_following: TrendFollowingSettings = Field(default_factory=TrendFollowingSettings)
    factor_investing: FactorInvestingSettings = Field(default_factory=FactorInvestingSettings)
    min_position_fraction: float = 0.01
    event_filter_enabled: bool = True


SUPPORTED_AUTO_TRADING_STRATEGIES = frozenset(
    {"dual_momentum", "trend_following", "factor_investing"}
)


def _validate_standard_cron(value: str, *, field_name: str) -> None:
    if len(value.split()) != 5:
        raise ConfigurationError(f"{field_name} must use standard 5-field cron syntax")


def _validate_time_range(value: str, *, field_name: str) -> None:
    parts = value.split("-")
    if len(parts) != 2:
        raise ConfigurationError(f"{field_name} must use HH:MM-HH:MM syntax")
    for part in parts:
        hour_minute = part.split(":")
        if len(hour_minute) != 2:
            raise ConfigurationError(f"{field_name} must use HH:MM-HH:MM syntax")
        try:
            hour = int(hour_minute[0])
            minute = int(hour_minute[1])
        except ValueError as exc:
            raise ConfigurationError(f"{field_name} must use HH:MM-HH:MM syntax") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ConfigurationError(f"{field_name} must use valid HH:MM values")


class AutoTradingMarketSettings(BaseModel):
    schedule_cron: str = "*/15 9-15 * * 1-5"
    strategy_schedule_crons: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_schedule_crons(self) -> "AutoTradingMarketSettings":
        _validate_standard_cron(self.schedule_cron, field_name="auto_trading market schedule_cron")

        unsupported_strategy_names = sorted(set(self.strategy_schedule_crons) - SUPPORTED_AUTO_TRADING_STRATEGIES)
        if unsupported_strategy_names:
            raise ConfigurationError(
                "auto_trading.kr.strategy_schedule_crons supports only "
                + ", ".join(sorted(SUPPORTED_AUTO_TRADING_STRATEGIES))
            )

        for strategy_name, cron in self.strategy_schedule_crons.items():
            _validate_standard_cron(
                cron,
                field_name=f"auto_trading.kr.strategy_schedule_crons.{strategy_name}",
            )
        return self

    def resolve_schedule_cron(self, strategy_name: str) -> str:
        return self.strategy_schedule_crons.get(strategy_name, self.schedule_cron)


class AutoTradingSettings(BaseModel):
    enabled: bool = False
    markets: list[str] = Field(default_factory=lambda: ["KR"])
    strategies: list[str] = Field(default_factory=lambda: ["dual_momentum", "trend_following"])
    max_orders_per_cycle: int = 1
    max_order_notional_per_cycle: float = 500000.0
    allow_new_entries: bool = True
    allow_exits: bool = True
    kr: AutoTradingMarketSettings = Field(default_factory=AutoTradingMarketSettings)

    @model_validator(mode="after")
    def validate_supported_scope(self) -> "AutoTradingSettings":
        if not self.markets:
            raise ConfigurationError("auto_trading.markets must not be empty")
        if len(set(self.markets)) != len(self.markets):
            raise ConfigurationError("auto_trading.markets must not contain duplicates")
        if set(self.markets) != {"KR"}:
            raise ConfigurationError("Phase 4 initial auto_trading scope supports KR only")
        if not self.strategies:
            raise ConfigurationError("auto_trading.strategies must not be empty")
        if len(set(self.strategies)) != len(self.strategies):
            raise ConfigurationError("auto_trading.strategies must not contain duplicates")
        if set(self.strategies) - SUPPORTED_AUTO_TRADING_STRATEGIES:
            raise ConfigurationError(
                "KR auto_trading scope supports only dual_momentum, trend_following, and factor_investing"
            )
        if self.max_orders_per_cycle < 1:
            raise ConfigurationError("auto_trading.max_orders_per_cycle must be at least 1")
        if self.max_order_notional_per_cycle <= 0:
            raise ConfigurationError("auto_trading.max_order_notional_per_cycle must be positive")
        return self


class Settings(BaseModel):
    env: RuntimeEnv = RuntimeEnv.VTS
    allocation: AllocationSettings = Field(default_factory=AllocationSettings)
    strategy_weights: StrategyWeightsSettings = Field(default_factory=StrategyWeightsSettings)
    strategies: StrategySettings = Field(default_factory=StrategySettings)
    auto_trading: AutoTradingSettings = Field(default_factory=AutoTradingSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
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


def _resolve_telegram_credentials(monitor_config: dict[str, Any], env_values: dict[str, str]) -> dict[str, Any]:
    telegram_config = monitor_config.get("telegram")
    if not isinstance(telegram_config, dict):
        return monitor_config

    token_name = telegram_config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    chat_id_name = telegram_config.get("chat_id_env", "TELEGRAM_CHAT_ID")
    bot_token = env_values.get(token_name, "").strip()
    chat_id = env_values.get(chat_id_name, "").strip()

    if not bot_token or not chat_id:
        return {
            **monitor_config,
            "telegram": {
                **telegram_config,
                "credentials": None,
            },
        }

    return {
        **monitor_config,
        "telegram": {
            **telegram_config,
            "credentials": {
                "bot_token": bot_token,
                "chat_id": chat_id,
            },
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
        "monitor": _resolve_telegram_credentials(config_data.get("monitor", {}), env_values),
        "kis": _resolve_kis_credentials(kis_config, env_values),
    }
    return Settings.model_validate(config_data)
