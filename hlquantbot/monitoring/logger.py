"""Logging configuration for HLQuantBot."""

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from ..config.settings import Settings


def setup_logging(
    settings: Optional[Settings] = None,
    log_level: str = "INFO",
    log_file: Optional[str] = None,
) -> logging.Logger:
    """
    Setup logging for HLQuantBot.

    Args:
        settings: Application settings
        log_level: Logging level
        log_file: Optional log file path

    Returns:
        Root logger
    """
    # Determine environment
    if settings:
        env = "TESTNET" if settings.is_testnet else "PROD"
    else:
        env = "UNKNOWN"

    # Create formatters
    detailed_formatter = logging.Formatter(
        f"%(asctime)s [{env}] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    simple_formatter = logging.Formatter(
        f"%(asctime)s [{env}] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Clear existing handlers
    root_logger.handlers = []

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    root_logger.addHandler(console_handler)

    # File handler with rotation (if specified)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Rotating file handler: 10MB max, keep 5 backups
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(detailed_formatter)
        root_logger.addHandler(file_handler)

    # Set levels for noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    # Get HLQuantBot logger
    bot_logger = logging.getLogger("hlquantbot")
    bot_logger.setLevel(logging.DEBUG)

    return bot_logger


class TradeLogger:
    """Specialized logger for trade events."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Trade log file with rotation
        self.trade_log = self.log_dir / "trades.log"
        self._trade_handler = RotatingFileHandler(
            self.trade_log,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=10,  # Keep more history for trades
            encoding="utf-8",
        )
        self._trade_handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )

        self._logger = logging.getLogger("hlquantbot.trades")
        self._logger.addHandler(self._trade_handler)
        self._logger.setLevel(logging.INFO)

    def log_trade(
        self,
        action: str,
        symbol: str,
        side: str,
        size: float,
        price: float,
        strategy: str,
        pnl: Optional[float] = None,
        reason: Optional[str] = None,
    ):
        """Log a trade event."""
        parts = [
            action.upper(),
            symbol,
            side.upper(),
            f"size={size:.6f}",
            f"price={price:.2f}",
            f"strategy={strategy}",
        ]

        if pnl is not None:
            parts.append(f"pnl={pnl:+.2f}")
        if reason:
            parts.append(f"reason={reason}")

        self._logger.info(" | ".join(parts))
