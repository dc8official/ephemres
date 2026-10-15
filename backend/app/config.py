from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class ConfigurationError(Exception):
    """Raised when application configuration is missing, invalid, or cannot be parsed."""
    pass


class DatabaseSettings(BaseModel):
    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "netmon"
    user: str = "netmon_user"
    password: str = "postgres"
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=10, ge=0, le=100)

    model_config = ConfigDict(extra="ignore")


class MonitoringSettings(BaseModel):
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    ping_count: int = Field(default=10, ge=1, le=20)
    ping_interval_seconds: int = Field(default=6, ge=1, le=60)
    state_confirmation_cycles: int = Field(default=3, ge=1, le=10)
    retention_days: int = Field(default=730, ge=30, le=3650)

    model_config = ConfigDict(extra="ignore")


class ApiSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    allowed_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )

    model_config = ConfigDict(extra="ignore")


class SecuritySettings(BaseModel):
    session_timeout_minutes: int = Field(default=120, ge=1, le=1440)
    max_active_sessions_per_user: int = Field(default=2, ge=1, le=10)
    hsts_enabled: bool = False
    secret_key: str = Field(default="dev-secret-key-change-in-production-min-32-chars")
    jwt_secret: Optional[str] = None
    crypto_key: Optional[str] = None
    fallback_crypto_keys: List[str] = Field(default_factory=list)

    @property
    def effective_jwt_secret(self) -> str:
        return self.jwt_secret or self.secret_key

    @effective_jwt_secret.setter
    def effective_jwt_secret(self, val: str) -> None:
        self.jwt_secret = val

    @property
    def effective_crypto_key(self) -> str:
        return self.crypto_key or self.secret_key

    @effective_crypto_key.setter
    def effective_crypto_key(self, val: str) -> None:
        self.crypto_key = val

    model_config = ConfigDict(extra="ignore")


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "/var/log/ephemres"

    model_config = ConfigDict(extra="ignore")


class RedisSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    password: Optional[str] = None
    enabled: bool = True
    performance_mode: bool = False

    model_config = ConfigDict(extra="ignore")


class FlowSettings(BaseModel):
    enabled: bool = False
    netflow_port: int = Field(default=2055, ge=1, le=65535)
    ipfix_port: int = Field(default=4739, ge=1, le=65535)
    sampling_multiplier: int = Field(default=1, ge=1)
    buffer_memory_limit_mb: int = Field(default=512, ge=64)

    model_config = ConfigDict(extra="ignore")


def resolve_config_file() -> Optional[Path]:
    """Resolves configuration file path from environment or standard locations."""
    env_path_str = os.environ.get("EPHEMRES_CONFIG_PATH") or os.environ.get("NETMON_CONFIG_PATH")
    if env_path_str:
        p = Path(env_path_str)
        if not p.is_file():
            var_name = "EPHEMRES_CONFIG_PATH" if os.environ.get("EPHEMRES_CONFIG_PATH") else "NETMON_CONFIG_PATH"
            raise ConfigurationError(
                f"Config file specified by {var_name} does not exist: {p}"
            )
        return p

    for prod_candidate in ("/etc/ephemres/config.toml", "/etc/netmon/config.toml"):
        prod_path = Path(prod_candidate)
        if prod_path.is_file():
            return prod_path

    # Development fallback in repository deploy/
    repo_fallback = (
        Path(__file__).resolve().parent.parent.parent
        / "deploy"
        / "config.template.toml"
    )
    if repo_fallback.is_file():
        return repo_fallback

    local_toml = Path("config.toml")
    if local_toml.is_file():
        return local_toml

    return None


class Settings(BaseSettings):
    environment: str = Field(default="development")
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    flow: FlowSettings = Field(default_factory=FlowSettings)

    model_config = SettingsConfigDict(
        env_prefix="EPHEMRES_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        cfg_file = resolve_config_file()
        if cfg_file is not None:
            try:
                toml_source = TomlConfigSettingsSource(
                    settings_cls,
                    toml_file=cfg_file,
                )
                return (
                    init_settings,
                    env_settings,
                    dotenv_settings,
                    toml_source,
                    file_secret_settings,
                )
            except Exception as e:
                raise ConfigurationError(
                    f"Failed to parse TOML configuration file at {cfg_file}: {e}"
                ) from e
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.database.user}:{self.database.password}@"
            f"{self.database.host}:{self.database.port}/{self.database.name}"
        )

    @property
    def sync_db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.database.user}:{self.database.password}@"
            f"{self.database.host}:{self.database.port}/{self.database.name}"
        )


def load_settings() -> Settings:
    """Load settings with environment variable fallbacks and validation."""
    try:
        loaded = Settings()
    except ValidationError as err:
        raise ConfigurationError(f"Configuration validation failed:\n{err}") from err
    except Exception as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(f"Error loading configuration: {exc}") from exc

    # Environment variable overrides (supporting EPHEMRES_ and legacy NETMON_)
    db_pass = os.environ.get("EPHEMRES_DB_PASSWORD") or os.environ.get("NETMON_DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    if db_pass:
        loaded.database.password = db_pass

    db_user = os.environ.get("EPHEMRES_DB_USER") or os.environ.get("NETMON_DB_USER") or os.environ.get("POSTGRES_USER")
    if db_user:
        loaded.database.user = db_user

    db_host = os.environ.get("EPHEMRES_DB_HOST") or os.environ.get("NETMON_DB_HOST") or os.environ.get("POSTGRES_HOST")
    if db_host:
        loaded.database.host = db_host

    db_port = os.environ.get("EPHEMRES_DB_PORT") or os.environ.get("NETMON_DB_PORT") or os.environ.get("POSTGRES_PORT")
    if db_port:
        try:
            loaded.database.port = int(db_port)
        except ValueError:
            raise ConfigurationError(f"Invalid integer for database port: {db_port}")

    db_name = os.environ.get("EPHEMRES_DB_NAME") or os.environ.get("NETMON_DB_NAME") or os.environ.get("POSTGRES_DB")
    if db_name:
        loaded.database.name = db_name

    db_pool = os.environ.get("EPHEMRES_DB_POOL_SIZE") or os.environ.get("NETMON_DB_POOL_SIZE")
    if db_pool:
        try:
            loaded.database.pool_size = int(db_pool)
        except ValueError:
            pass

    db_overflow = os.environ.get("EPHEMRES_DB_MAX_OVERFLOW") or os.environ.get("NETMON_DB_MAX_OVERFLOW")
    if db_overflow:
        try:
            loaded.database.max_overflow = int(db_overflow)
        except ValueError:
            pass

    secret_key = os.environ.get("EPHEMRES_SECRET_KEY") or os.environ.get("NETMON_SECRET_KEY")
    if secret_key:
        loaded.security.secret_key = secret_key

    jwt_secret = os.environ.get("EPHEMRES_JWT_SECRET") or os.environ.get("NETMON_JWT_SECRET")
    if jwt_secret:
        loaded.security.jwt_secret = jwt_secret

    crypto_key = os.environ.get("EPHEMRES_CRYPTO_KEY") or os.environ.get("NETMON_CRYPTO_KEY")
    if crypto_key:
        loaded.security.crypto_key = crypto_key

    fallback_keys = os.environ.get("EPHEMRES_FALLBACK_CRYPTO_KEYS") or os.environ.get("NETMON_FALLBACK_CRYPTO_KEYS")
    if fallback_keys:
        loaded.security.fallback_crypto_keys = [
            k.strip() for k in fallback_keys.split(",") if k.strip()
        ]

    env_val = os.environ.get("EPHEMRES_ENV") or os.environ.get("NETMON_ENV") or os.environ.get("ENVIRONMENT")
    if env_val:
        loaded.environment = env_val.strip().lower()

    allowed_origins = os.environ.get("EPHEMRES_API__ALLOWED_ORIGINS") or os.environ.get("NETMON_API__ALLOWED_ORIGINS")
    if allowed_origins:
        import json
        try:
            loaded.api.allowed_origins = json.loads(allowed_origins)
        except Exception:
            pass

    # FIX-09 (SEC-02): Reject insecure default secret key in production / staging
    if loaded.environment.lower() not in ("development", "test"):
        if (
            loaded.security.secret_key == "dev-secret-key-change-in-production-min-32-chars"
            or loaded.security.effective_jwt_secret == "dev-secret-key-change-in-production-min-32-chars"
            or loaded.security.effective_crypto_key == "dev-secret-key-change-in-production-min-32-chars"
        ):
            raise ValueError(
                "Insecure configuration: Secret keys must be changed from the default development secret in non-development environments."
            )

    # FIX-10 (SEC-04): Validate that allowed_origins does not contain "*" when allow_credentials=True
    if "*" in loaded.api.allowed_origins:
        raise ValueError(
            "CORS configuration error: allowed_origins cannot contain '*' when allow_credentials=True."
        )

    return loaded


settings = load_settings()
