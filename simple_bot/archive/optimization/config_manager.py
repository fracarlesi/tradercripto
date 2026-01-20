"""
Hot-Reload Configuration Manager
Manages strategy parameters with database versioning and hot-reload capability.
"""

import asyncio
import yaml
import json
from pathlib import Path
from typing import Dict, Optional, Callable, List, Any
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


@dataclass
class ConfigSnapshot:
    """Snapshot of configuration at a point in time."""
    version_id: int
    params: Dict
    applied_at: datetime
    source: str


class HotReloadConfigManager:
    """
    Manages configuration with hot-reload capability.

    Features:
    - Loads config from YAML at startup
    - Stores parameter versions in PostgreSQL
    - Notifies listeners when config changes
    - Supports rollback to previous versions
    """

    def __init__(self, config_path: str, db):
        """
        Initialize config manager.

        Args:
            config_path: Path to multi_config.yaml
            db: Database connection (asyncpg pool)
        """
        self.config_path = Path(config_path)
        self.db = db
        self._current_config: Optional[Dict] = None
        self._current_version: Optional[int] = None
        self._lock = asyncio.Lock()
        self._listeners: List[Callable[[Dict], Any]] = []
        self._last_reload_time: Optional[datetime] = None

    async def initialize(self) -> int:
        """
        Load initial config and sync with database.
        Returns the active version ID.
        """
        # Load from YAML
        self._current_config = self._load_yaml()

        async with self.db.acquire() as conn:
            # Check if we have an active version in DB
            row = await conn.fetchrow("""
                SELECT * FROM parameter_versions
                WHERE is_active = TRUE
                ORDER BY created_at DESC
                LIMIT 1
            """)

            if row:
                # Use DB version
                self._current_version = row['version_id']
                self._current_config = self._row_to_config(row)
                logger.info(f"Loaded active parameter version {self._current_version} from DB")
            else:
                # Create initial version from YAML
                self._current_version = await self._save_version_to_db(
                    self._current_config,
                    source="initial"
                )
                logger.info(f"Created initial parameter version {self._current_version} from YAML")

        self._last_reload_time = datetime.now(timezone.utc)
        return self._current_version

    def _load_yaml(self) -> Dict:
        """Load config from YAML file."""
        with open(self.config_path) as f:
            data = yaml.safe_load(f)

        # Normalize to standard structure
        return {
            "tp_pct": data.get("tp_pct", 0.01),
            "sl_pct": data.get("sl_pct", 0.005),
            "position_size_usd": data.get("position_size_usd", 100),
            "leverage": data.get("leverage", 5),
            "momentum": {
                "enabled": data.get("momentum", {}).get("enabled", True),
                "ema_fast": data.get("momentum", {}).get("ema_fast", 20),
                "ema_slow": data.get("momentum", {}).get("ema_slow", 50),
                "rsi_period": data.get("momentum", {}).get("rsi_period", 14),
                "rsi_long_threshold": data.get("momentum", {}).get("rsi_long_threshold", 55),
                "rsi_short_threshold": data.get("momentum", {}).get("rsi_short_threshold", 45),
            },
            "mean_reversion": {
                "enabled": data.get("mean_reversion", {}).get("enabled", True),
                "rsi_oversold": data.get("mean_reversion", {}).get("rsi_oversold", 30),
                "rsi_overbought": data.get("mean_reversion", {}).get("rsi_overbought", 70),
                "bb_period": data.get("mean_reversion", {}).get("bb_period", 20),
                "bb_std": data.get("mean_reversion", {}).get("bb_std", 2.0),
            },
            "breakout": {
                "enabled": data.get("breakout", {}).get("enabled", True),
                "lookback_bars": data.get("breakout", {}).get("lookback_bars", 20),
                "min_breakout_pct": data.get("breakout", {}).get("min_breakout_pct", 0.002),
            },
        }

    def _row_to_config(self, row) -> Dict:
        """Convert database row to config dict."""
        return {
            "tp_pct": float(row['tp_pct']),
            "sl_pct": float(row['sl_pct']),
            "position_size_usd": float(row['position_size_usd']),
            "leverage": row['leverage'],
            "momentum": {
                "enabled": row['momentum_enabled'],
                "ema_fast": row['momentum_ema_fast'],
                "ema_slow": row['momentum_ema_slow'],
                "rsi_period": row['momentum_rsi_period'],
                "rsi_long_threshold": row['momentum_rsi_long'],
                "rsi_short_threshold": row['momentum_rsi_short'],
            },
            "mean_reversion": {
                "enabled": row['meanrev_enabled'],
                "rsi_oversold": row['meanrev_rsi_oversold'],
                "rsi_overbought": row['meanrev_rsi_overbought'],
                "bb_period": row['meanrev_bb_period'],
                "bb_std": float(row['meanrev_bb_std']),
            },
            "breakout": {
                "enabled": row['breakout_enabled'],
                "lookback_bars": row['breakout_lookback'],
                "min_breakout_pct": float(row['breakout_min_pct']),
            },
        }

    async def _save_version_to_db(
        self,
        config: Dict,
        source: str,
        reasoning: str = None
    ) -> int:
        """Save a new parameter version to database."""
        async with self.db.acquire() as conn:
            async with conn.transaction():
                # Deactivate current active version
                await conn.execute("""
                    UPDATE parameter_versions SET is_active = FALSE WHERE is_active = TRUE
                """)

                # Insert new version
                version_id = await conn.fetchval("""
                    INSERT INTO parameter_versions (
                        source, llm_reasoning,
                        tp_pct, sl_pct, position_size_usd, leverage,
                        momentum_enabled, momentum_ema_fast, momentum_ema_slow,
                        momentum_rsi_period, momentum_rsi_long, momentum_rsi_short,
                        meanrev_enabled, meanrev_rsi_oversold, meanrev_rsi_overbought,
                        meanrev_bb_period, meanrev_bb_std,
                        breakout_enabled, breakout_lookback, breakout_min_pct,
                        is_active, applied_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18, $19, $20, TRUE, NOW()
                    ) RETURNING version_id
                """,
                    source, reasoning,
                    config['tp_pct'], config['sl_pct'],
                    config['position_size_usd'], config['leverage'],
                    config['momentum']['enabled'],
                    config['momentum']['ema_fast'], config['momentum']['ema_slow'],
                    config['momentum']['rsi_period'],
                    config['momentum']['rsi_long_threshold'],
                    config['momentum']['rsi_short_threshold'],
                    config['mean_reversion']['enabled'],
                    config['mean_reversion']['rsi_oversold'],
                    config['mean_reversion']['rsi_overbought'],
                    config['mean_reversion']['bb_period'],
                    config['mean_reversion']['bb_std'],
                    config['breakout']['enabled'],
                    config['breakout']['lookback_bars'],
                    config['breakout']['min_breakout_pct']
                )

                return version_id

    @property
    def current_config(self) -> Dict:
        """Get current configuration (thread-safe copy)."""
        return self._current_config.copy() if self._current_config else {}

    @property
    def current_version(self) -> int:
        """Get current parameter version ID."""
        return self._current_version or 0

    def register_listener(self, callback: Callable[[Dict], Any]):
        """
        Register a callback to be notified of config changes.

        Args:
            callback: Function that takes new config dict. Can be async.
        """
        self._listeners.append(callback)
        logger.debug(f"Registered config listener: {callback.__name__}")

    async def apply_new_config(
        self,
        new_config: Dict,
        source: str = "manual",
        reasoning: str = None
    ) -> int:
        """
        Apply new configuration with hot-reload.

        Args:
            new_config: New parameter dictionary
            source: Source of change ('llm', 'manual', 'rollback')
            reasoning: Optional reasoning (from LLM)

        Returns:
            New version ID
        """
        async with self._lock:
            # Save to database
            version_id = await self._save_version_to_db(new_config, source, reasoning)

            # Update in memory
            self._current_config = new_config
            self._current_version = version_id
            self._last_reload_time = datetime.now(timezone.utc)

            logger.info(f"Applied new config version {version_id} (source: {source})")

        # Notify all listeners (outside lock to avoid deadlock)
        await self._notify_listeners(new_config)

        # Optionally update YAML file
        await self._update_yaml_file(new_config)

        return version_id

    async def _notify_listeners(self, new_config: Dict):
        """Notify all registered listeners of config change."""
        for listener in self._listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(new_config)
                else:
                    listener(new_config)
            except Exception as e:
                logger.error(f"Error notifying listener {listener.__name__}: {e}")

    async def _update_yaml_file(self, config: Dict):
        """Update YAML file with new config (for persistence)."""
        try:
            # Load current YAML to preserve comments and extra fields
            current = {}
            if self.config_path.exists():
                with open(self.config_path) as f:
                    current = yaml.safe_load(f) or {}

            # Update values
            current['tp_pct'] = config['tp_pct']
            current['sl_pct'] = config['sl_pct']
            current['position_size_usd'] = config['position_size_usd']
            current['leverage'] = config['leverage']

            for strategy in ['momentum', 'mean_reversion', 'breakout']:
                if strategy not in current:
                    current[strategy] = {}
                for key, value in config[strategy].items():
                    current[strategy][key] = value

            # Write back
            with open(self.config_path, 'w') as f:
                yaml.dump(current, f, default_flow_style=False, sort_keys=False)

            logger.debug(f"Updated YAML file: {self.config_path}")

        except Exception as e:
            logger.warning(f"Failed to update YAML file: {e}")

    async def rollback_to_version(self, version_id: int) -> bool:
        """
        Rollback to a specific parameter version.

        Args:
            version_id: Version ID to rollback to

        Returns:
            True if successful, False if version not found
        """
        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM parameter_versions WHERE version_id = $1
            """, version_id)

            if not row:
                logger.error(f"Version {version_id} not found for rollback")
                return False

            config = self._row_to_config(row)
            await self.apply_new_config(
                config,
                source="rollback",
                reasoning=f"Rollback to version {version_id}"
            )

            # Mark original as reverted
            await conn.execute("""
                UPDATE parameter_versions
                SET reverted_at = NOW()
                WHERE version_id = $1
            """, self._current_version)

            return True

    async def get_version_history(self, limit: int = 10) -> List[Dict]:
        """Get recent parameter version history with performance."""
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM parameter_performance
                ORDER BY created_at DESC
                LIMIT $1
            """, limit)

            return [dict(r) for r in rows]

    async def get_best_performing_version(self, exclude_current: bool = True) -> Optional[int]:
        """Get the best performing version ID based on hourly P&L."""
        async with self.db.acquire() as conn:
            query = """
                SELECT version_id
                FROM parameter_performance
                WHERE hours_active >= 4
                  AND total_trades >= 5
            """
            if exclude_current:
                query += f" AND version_id != {self._current_version}"

            query += " ORDER BY hourly_pnl_avg DESC LIMIT 1"

            row = await conn.fetchrow(query)
            return row['version_id'] if row else None
