from __future__ import annotations
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

def setup_logging(service_name: str = "ephemres-api", log_dir: Optional[str] = None, log_level: str = "INFO") -> logging.Logger:
    """
    Configures unified dual console (systemd journal) and auto-rotating file handlers
    with strict ~150MB total storage bounding across the platform.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers if setup_logging is called multiple times
    if root_logger.handlers:
        root_logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. Console Handler (Systemd Journal / Stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 2. Determine target log directory
    target_dir = Path(log_dir) if log_dir else Path("/var/log/ephemres")
    use_file_logging = False

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # Test write permissions
        test_file = target_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
        use_file_logging = True
    except Exception:
        # Fallback to local logs directory for dev/unprivileged environments
        fallback_dir = Path(os.getcwd()) / "logs"
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            target_dir = fallback_dir
            use_file_logging = True
        except Exception:
            use_file_logging = False

    if use_file_logging:
        try:
            # Main Service Rotating Log File (Max 10MB x 5 backups = 60MB)
            service_log_path = target_dir / f"{service_name}.log"
            service_file_handler = RotatingFileHandler(
                filename=str(service_log_path),
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8"
            )
            service_file_handler.setLevel(level)
            service_file_handler.setFormatter(formatter)
            root_logger.addHandler(service_file_handler)

            # Dedicated Error-Only Log File (Max 10MB x 3 backups = 30MB)
            error_log_path = target_dir / "error.log"
            error_file_handler = RotatingFileHandler(
                filename=str(error_log_path),
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=3,
                encoding="utf-8"
            )
            error_file_handler.setLevel(logging.ERROR)
            error_file_handler.setFormatter(formatter)
            root_logger.addHandler(error_file_handler)
        except Exception as e:
            console_handler.handle(
                logging.LogRecord(
                    name="logging_config",
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=75,
                    msg=f"Failed to initialize rotating file loggers at {target_dir}: {e}",
                    args=(),
                    exc_info=None
                )
            )

    logger = logging.getLogger(service_name)
    logger.info("Initialized %s logging (Dual Console + Rotating File Handlers in %s)", service_name, target_dir)
    return logger
