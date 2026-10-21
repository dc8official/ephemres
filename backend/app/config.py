from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tomllib
from typing import Any, Dict, List, Literal, Optional


class ConfigurationError(Exception):
    """Raised when application configuration is missing, invalid, or cannot be parsed."""
    pass


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    raise ConfigurationError(f"Invalid boolean value: {val!r}")


def _coerce_int(val: Any, name: str, ge: Optional[int] = None, le: Optional[int] = None) -> int:
    if isinstance(val, bool):
        raise ConfigurationError(f"Invalid integer for {name}: {val!r}")
    try:
        res = int(val)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"Invalid integer for {name}: {val}") from exc
    if ge is not None and res < ge:
        raise ConfigurationError(f"Configuration validation failed: {name}={res} must be >= {ge}")
    if le is not None and res > le:
        raise ConfigurationError(f"Configuration validation failed: {name}={res} must be <= {le}")
    return res


def _coerce_str_list(val: Any, name: str) -> List[str]:
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except Exception as exc:
                raise ConfigurationError(f"Invalid list JSON for {name}: {val}") from exc
        return [x.strip() for x in s.split(",") if x.strip()]
    raise ConfigurationError(f"Invalid list value for {name}: {val!r}")


@dataclass
class DatabaseSettings:
    host: str = "localhost"
    port: int = 5432
    name: str = "netmon"
    user: str = "netmon_user"
    password: str = "postgres"
    pool_size: int = 10
    max_overflow: int = 10

    def __post_init__(self) -> None:
        self.host = str(self.host)
        self.port = _coerce_int(self.port, "database.port", ge=1, le=65535)
        self.name = str(self.name)
        self.user = str(self.user)
        self.password = str(self.password)
        self.pool_size = _coerce_int(self.pool_size, "database.pool_size", ge=1, le=100)
        self.max_overflow = _coerce_int(self.max_overflow, "database.max_overflow", ge=0, le=100)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MonitoringSettings:
    interval_seconds: int = 60
    # Worst-case sweep (count * 2s timeout + (count - 1) * interval) must fit the 60s cycle; see monitoring/engine.py.
    ping_count: int = 5
    ping_interval_seconds: int = 8
    state_confirmation_cycles: int = 3
    retention_days: int = 730

    def __post_init__(self) -> None:
        self.interval_seconds = _coerce_int(self.interval_seconds, "monitoring.interval_seconds", ge=10, le=3600)
        self.ping_count = _coerce_int(self.ping_count, "monitoring.ping_count", ge=1, le=20)
        self.ping_interval_seconds = _coerce_int(self.ping_interval_seconds, "monitoring.ping_interval_seconds", ge=1, le=60)
        self.state_confirmation_cycles = _coerce_int(self.state_confirmation_cycles, "monitoring.state_confirmation_cycles", ge=1, le=10)
        self.retention_days = _coerce_int(self.retention_days, "monitoring.retention_days", ge=30, le=3650)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ApiSettings:
    host: str = "0.0.0.0"
    port: int = 8000
    allowed_origins: List[str] = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )

    def __post_init__(self) -> None:
        self.host = str(self.host)
        self.port = _coerce_int(self.port, "api.port", ge=1, le=65535)
        self.allowed_origins = _coerce_str_list(self.allowed_origins, "api.allowed_origins")

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SecuritySettings:
    session_timeout_minutes: int = 120
    max_active_sessions_per_user: int = 2
    hsts_enabled: bool = False
    secret_key: str = "dev-secret-key-change-in-production-min-32-chars"
    jwt_secret: Optional[str] = None
    crypto_key: Optional[str] = None
    fallback_crypto_keys: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.session_timeout_minutes = _coerce_int(
            self.session_timeout_minutes, "security.session_timeout_minutes", ge=1, le=1440
        )
        self.max_active_sessions_per_user = _coerce_int(
            self.max_active_sessions_per_user, "security.max_active_sessions_per_user", ge=1, le=10
        )
        self.hsts_enabled = _coerce_bool(self.hsts_enabled)
        self.secret_key = str(self.secret_key)
        if self.jwt_secret is not None:
            self.jwt_secret = str(self.jwt_secret)
        if self.crypto_key is not None:
            self.crypto_key = str(self.crypto_key)
        self.fallback_crypto_keys = _coerce_str_list(self.fallback_crypto_keys, "security.fallback_crypto_keys")

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

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LoggingSettings:
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "/var/log/ephemres"

    def __post_init__(self) -> None:
        lvl = str(self.level).upper()
        if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigurationError(f"Invalid logging level: {self.level}")
        self.level = lvl  # type: ignore[assignment]
        self.log_dir = str(self.log_dir)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FastStoreSettings:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    enabled: bool = True
    performance_mode: bool = False

    def __post_init__(self) -> None:
        self.host = str(self.host)
        self.port = _coerce_int(self.port, "faststore.port", ge=1, le=65535)
        self.db = _coerce_int(self.db, "faststore.db", ge=0, le=15)
        if self.password is not None:
            self.password = str(self.password)
        self.enabled = _coerce_bool(self.enabled)
        self.performance_mode = _coerce_bool(self.performance_mode)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


RedisSettings = FastStoreSettings


@dataclass
class FlowSettings:
    enabled: bool = False
    bind_host: str = "0.0.0.0"
    netflow_port: int = 2055
    ipfix_port: int = 4739
    sampling_multiplier: int = 1
    buffer_memory_limit_mb: int = 512

    def __post_init__(self) -> None:
        self.enabled = _coerce_bool(self.enabled)
        self.bind_host = str(self.bind_host).strip() or "0.0.0.0"
        self.netflow_port = _coerce_int(self.netflow_port, "flow.netflow_port", ge=1, le=65535)
        self.ipfix_port = _coerce_int(self.ipfix_port, "flow.ipfix_port", ge=1, le=65535)
        self.sampling_multiplier = _coerce_int(self.sampling_multiplier, "flow.sampling_multiplier", ge=1)
        self.buffer_memory_limit_mb = _coerce_int(self.buffer_memory_limit_mb, "flow.buffer_memory_limit_mb", ge=64)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


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


def _parse_dotenv_file(dotenv_path: Path) -> Dict[str, str]:
    """Lightweight stdlib .env parser."""
    env_vars: Dict[str, str] = {}
    if not dotenv_path.is_file():
        return env_vars
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                    v = v[1:-1]
                if k:
                    env_vars[k] = v
    except Exception:
        pass
    return env_vars


def _load_merged_env() -> Dict[str, str]:
    """Merges .env (EPHEMRES_* variables) with os.environ (os.environ takes precedence)."""
    merged: Dict[str, str] = {}
    for candidate in (
        Path(__file__).resolve().parent.parent.parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path(".env"),
    ):
        if candidate.is_file():
            for k, v in _parse_dotenv_file(candidate).items():
                if k.upper().startswith("EPHEMRES_"):
                    merged[k] = v
    merged.update(os.environ)
    return merged


def _build_section(cls: type, toml_data: Dict[str, Any], section_name: str, env_map: Dict[str, str]) -> Any:
    raw_section = dict(toml_data.get(section_name, {})) if isinstance(toml_data.get(section_name), dict) else {}
    if not raw_section and section_name == "faststore" and "redis" in toml_data:
        raw_section = dict(toml_data.get("redis", {})) if isinstance(toml_data.get("redis"), dict) else {}
    valid_fields = set(cls.__dataclass_fields__.keys())  # type: ignore[attr-defined]
    kwargs: Dict[str, Any] = {k: v for k, v in raw_section.items() if k in valid_fields}

    # Apply nested delimiter overrides: EPHEMRES_<SECTION>__<FIELD> and NETMON_<SECTION>__<FIELD>
    sections_to_check = [section_name]
    if section_name in ("faststore", "redis"):
        sections_to_check = ["faststore", "redis"]
    for s_name in sections_to_check:
        for prefix in ("EPHEMRES_", "NETMON_"):
            sec_prefix = f"{prefix}{s_name.upper()}__"
            for env_k, env_v in env_map.items():
                if env_k.upper().startswith(sec_prefix):
                    field_name = env_k[len(sec_prefix):].lower()
                    if field_name in valid_fields:
                        kwargs[field_name] = env_v

    return cls(**kwargs)


class Settings:
    """Zero-dependency application settings loader using stdlib tomllib and os.environ."""

    def __init__(
        self,
        environment: Optional[str] = None,
        database: Optional[DatabaseSettings] = None,
        monitoring: Optional[MonitoringSettings] = None,
        api: Optional[ApiSettings] = None,
        security: Optional[SecuritySettings] = None,
        logging: Optional[LoggingSettings] = None,
        faststore: Optional[FastStoreSettings] = None,
        redis: Optional[FastStoreSettings] = None,
        flow: Optional[FlowSettings] = None,
        _toml_file: Optional[Path] = None,
    ) -> None:
        cfg_file = _toml_file if _toml_file is not None else resolve_config_file()
        toml_data: Dict[str, Any] = {}
        if cfg_file is not None:
            try:
                with open(cfg_file, "rb") as f:
                    toml_data = tomllib.load(f)
            except Exception as e:
                raise ConfigurationError(
                    f"Failed to parse TOML configuration file at {cfg_file}: {e}"
                ) from e

        env_map = _load_merged_env()

        env_from_toml = toml_data.get("environment", "development")
        env_from_vars = (
            env_map.get("EPHEMRES_ENVIRONMENT")
            or env_map.get("NETMON_ENVIRONMENT")
            or env_map.get("EPHEMRES_ENV")
            or env_map.get("NETMON_ENV")
            or env_map.get("ENVIRONMENT")
        )
        resolved_env = environment or env_from_vars or env_from_toml or "development"
        self.environment: str = str(resolved_env).strip().lower()

        self.database: DatabaseSettings = database or _build_section(DatabaseSettings, toml_data, "database", env_map)
        self.monitoring: MonitoringSettings = monitoring or _build_section(MonitoringSettings, toml_data, "monitoring", env_map)
        self.api: ApiSettings = api or _build_section(ApiSettings, toml_data, "api", env_map)
        self.security: SecuritySettings = security or _build_section(SecuritySettings, toml_data, "security", env_map)
        self.logging: LoggingSettings = logging or _build_section(LoggingSettings, toml_data, "logging", env_map)
        fs_section = "faststore" if "faststore" in toml_data else "redis"
        self.faststore: FastStoreSettings = (
            faststore
            or redis
            or _build_section(FastStoreSettings, toml_data, fs_section, env_map)
        )
        self.flow: FlowSettings = flow or _build_section(FlowSettings, toml_data, "flow", env_map)

        _apply_env_overrides_and_validate(self, env_map)

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.database.user}:{self.database.password}@"
            f"{self.database.host}:{self.database.port}/{self.database.name}"
        )

    @property
    def async_database_url(self) -> str:
        return self.db_url

    @property
    def sync_db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.database.user}:{self.database.password}@"
            f"{self.database.host}:{self.database.port}/{self.database.name}"
        )

    @property
    def sync_database_url(self) -> str:
        return self.sync_db_url

    @property
    def effective_jwt_secret(self) -> str:
        return self.security.effective_jwt_secret

    @property
    def effective_crypto_key(self) -> str:
        return self.security.effective_crypto_key

    @property
    def fallback_crypto_keys(self) -> List[str]:
        return self.security.fallback_crypto_keys

    @property
    def redis(self) -> FastStoreSettings:
        return self.faststore

    @redis.setter
    def redis(self, val: FastStoreSettings) -> None:
        self.faststore = val

    def model_dump(self) -> Dict[str, Any]:
        return {
            "environment": self.environment,
            "database": self.database.model_dump(),
            "monitoring": self.monitoring.model_dump(),
            "api": self.api.model_dump(),
            "security": self.security.model_dump(),
            "logging": self.logging.model_dump(),
            "faststore": self.faststore.model_dump(),
            "redis": self.redis.model_dump(),
            "flow": self.flow.model_dump(),
        }


def _apply_env_overrides_and_validate(loaded: Settings, env_map: Dict[str, str]) -> None:
    # Environment variable overrides (supporting EPHEMRES_ and legacy NETMON_)
    db_pass = env_map.get("EPHEMRES_DB_PASSWORD") or env_map.get("NETMON_DB_PASSWORD") or env_map.get("POSTGRES_PASSWORD")
    if db_pass:
        loaded.database.password = db_pass

    db_user = env_map.get("EPHEMRES_DB_USER") or env_map.get("NETMON_DB_USER") or env_map.get("POSTGRES_USER")
    if db_user:
        loaded.database.user = db_user

    db_host = env_map.get("EPHEMRES_DB_HOST") or env_map.get("NETMON_DB_HOST") or env_map.get("POSTGRES_HOST")
    if db_host:
        loaded.database.host = db_host

    db_port = env_map.get("EPHEMRES_DB_PORT") or env_map.get("NETMON_DB_PORT") or env_map.get("POSTGRES_PORT")
    if db_port:
        try:
            loaded.database.port = int(db_port)
        except ValueError as err:
            raise ConfigurationError(f"Invalid integer for database port: {db_port}") from err

    db_name = env_map.get("EPHEMRES_DB_NAME") or env_map.get("NETMON_DB_NAME") or env_map.get("POSTGRES_DB")
    if db_name:
        loaded.database.name = db_name

    db_pool = env_map.get("EPHEMRES_DB_POOL_SIZE") or env_map.get("NETMON_DB_POOL_SIZE")
    if db_pool:
        try:
            loaded.database.pool_size = int(db_pool)
        except ValueError:
            pass

    db_overflow = env_map.get("EPHEMRES_DB_MAX_OVERFLOW") or env_map.get("NETMON_DB_MAX_OVERFLOW")
    if db_overflow:
        try:
            loaded.database.max_overflow = int(db_overflow)
        except ValueError:
            pass

    secret_key = env_map.get("EPHEMRES_SECRET_KEY") or env_map.get("NETMON_SECRET_KEY")
    if secret_key:
        loaded.security.secret_key = secret_key

    jwt_secret = env_map.get("EPHEMRES_JWT_SECRET") or env_map.get("NETMON_JWT_SECRET")
    if jwt_secret:
        loaded.security.jwt_secret = jwt_secret

    crypto_key = env_map.get("EPHEMRES_CRYPTO_KEY") or env_map.get("NETMON_CRYPTO_KEY")
    if crypto_key:
        loaded.security.crypto_key = crypto_key

    fallback_keys = env_map.get("EPHEMRES_FALLBACK_CRYPTO_KEYS") or env_map.get("NETMON_FALLBACK_CRYPTO_KEYS")
    if fallback_keys:
        loaded.security.fallback_crypto_keys = [
            k.strip() for k in fallback_keys.split(",") if k.strip()
        ]

    env_val = env_map.get("EPHEMRES_ENV") or env_map.get("NETMON_ENV") or env_map.get("ENVIRONMENT")
    if env_val:
        loaded.environment = env_val.strip().lower()

    allowed_origins = (
        env_map.get("EPHEMRES_API__ALLOWED_ORIGINS")
        or env_map.get("NETMON_API__ALLOWED_ORIGINS")
        or env_map.get("EPHEMRES_ALLOWED_ORIGINS")
        or env_map.get("NETMON_ALLOWED_ORIGINS")
    )
    if allowed_origins:
        try:
            loaded.api.allowed_origins = _coerce_str_list(allowed_origins, "api.allowed_origins")
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


def load_settings() -> Settings:
    """Load settings with environment variable fallbacks and validation."""
    try:
        return Settings()
    except (ConfigurationError, ValueError):
        raise
    except Exception as exc:
        raise ConfigurationError(f"Error loading configuration: {exc}") from exc


settings = load_settings()
