"""
Safety Monitor and Automatic Rollback
Detects performance degradation and triggers rollback.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class SafetyMonitor:
    """
    Monitors performance after parameter changes.
    Triggers automatic rollback if performance degrades significantly.

    Rules:
    1. Monitor for minimum hours after any change before rollback
    2. Rollback if net P&L drops significantly from previous version
    3. Rollback if drawdown exceeds threshold
    4. Rollback if win rate drops below minimum
    """

    def __init__(
        self,
        db,
        config_manager,
        logger_instance: logging.Logger = None,
        min_monitoring_hours: int = 4,
        max_pnl_decline_pct: float = -50,
        max_drawdown_pct: float = 15,
        min_win_rate: float = 25,
        min_trades_for_evaluation: int = 10
    ):
        """
        Initialize safety monitor.

        Args:
            db: Database connection pool
            config_manager: HotReloadConfigManager instance
            logger_instance: Optional logger
            min_monitoring_hours: Minimum hours before rollback decision
            max_pnl_decline_pct: Rollback if P&L declines by this % vs previous
            max_drawdown_pct: Rollback if drawdown exceeds this %
            min_win_rate: Rollback if win rate below this %
            min_trades_for_evaluation: Minimum trades needed for evaluation
        """
        self.db = db
        self.config_manager = config_manager
        self.logger = logger_instance or logger

        # Thresholds (no safety limits as per user request - these are monitoring only)
        self.min_monitoring_hours = min_monitoring_hours
        self.max_pnl_decline_pct = max_pnl_decline_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.min_win_rate = min_win_rate
        self.min_trades_for_evaluation = min_trades_for_evaluation

    async def check_and_rollback(self) -> Tuple[bool, str]:
        """
        Check current performance and rollback if needed.

        Returns:
            (rolled_back: bool, reason: str)
        """
        current_version = self.config_manager.current_version

        # Get current version performance
        current_perf = await self._get_version_performance(current_version)

        if not current_perf:
            return False, "No performance data yet"

        if current_perf['hours_active'] < self.min_monitoring_hours:
            return False, f"Not enough monitoring time ({current_perf['hours_active']}h < {self.min_monitoring_hours}h)"

        if current_perf['trades'] < self.min_trades_for_evaluation:
            return False, f"Not enough trades ({current_perf['trades']} < {self.min_trades_for_evaluation})"

        # Get previous version performance for comparison
        previous_perf = await self._get_previous_version_performance(current_version)

        rollback_reason = None

        # Check condition 1: P&L decline vs previous version
        if previous_perf and previous_perf['hourly_pnl'] > 0:
            pnl_change_pct = (
                (current_perf['hourly_pnl'] - previous_perf['hourly_pnl'])
                / abs(previous_perf['hourly_pnl']) * 100
            )
            if pnl_change_pct < self.max_pnl_decline_pct:
                rollback_reason = (
                    f"P&L declined {pnl_change_pct:.1f}% vs previous version "
                    f"(${current_perf['hourly_pnl']:.4f}/hr vs ${previous_perf['hourly_pnl']:.4f}/hr)"
                )

        # Check condition 2: Absolute drawdown
        if current_perf['max_drawdown'] and current_perf['max_drawdown'] > self.max_drawdown_pct:
            rollback_reason = f"Drawdown {current_perf['max_drawdown']:.1f}% exceeds threshold ({self.max_drawdown_pct}%)"

        # Check condition 3: Win rate collapse
        if current_perf['win_rate'] < self.min_win_rate:
            rollback_reason = f"Win rate {current_perf['win_rate']:.1f}% below minimum ({self.min_win_rate}%)"

        if rollback_reason:
            self.logger.warning(f"SAFETY: Triggering rollback - {rollback_reason}")

            # Find best previous version to rollback to
            best_version = await self._find_best_previous_version(current_version)

            if best_version:
                self.logger.info(f"Rolling back from v{current_version} to v{best_version}")

                # Mark current version as reverted
                async with self.db.acquire() as conn:
                    await conn.execute("""
                        UPDATE parameter_versions
                        SET reverted_at = NOW()
                        WHERE version_id = $1
                    """, current_version)

                # Rollback
                await self.config_manager.rollback_to_version(best_version)

                return True, rollback_reason
            else:
                self.logger.warning("No suitable version to rollback to")
                return False, "Rollback needed but no suitable previous version"

        return False, "Performance acceptable"

    async def _get_version_performance(self, version_id: int) -> Optional[dict]:
        """Get aggregated performance for a version."""
        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) as hours_active,
                    COALESCE(SUM(trades_count), 0) as trades,
                    COALESCE(SUM(win_count), 0) as wins,
                    COALESCE(SUM(net_pnl), 0) as total_pnl,
                    AVG(max_drawdown_pct) as max_drawdown
                FROM hourly_metrics
                WHERE parameter_version = $1
            """, version_id)

            if not row or row['hours_active'] == 0:
                return None

            trades = int(row['trades'])
            wins = int(row['wins'])
            hours = int(row['hours_active'])

            return {
                'hours_active': hours,
                'trades': trades,
                'wins': wins,
                'win_rate': (wins / trades * 100) if trades > 0 else 0,
                'total_pnl': float(row['total_pnl']),
                'hourly_pnl': float(row['total_pnl']) / hours if hours > 0 else 0,
                'max_drawdown': float(row['max_drawdown']) if row['max_drawdown'] else 0
            }

    async def _get_previous_version_performance(self, current_version: int) -> Optional[dict]:
        """Get performance of the version before current."""
        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT version_id FROM parameter_versions
                WHERE version_id < $1
                ORDER BY version_id DESC
                LIMIT 1
            """, current_version)

            if row:
                return await self._get_version_performance(row['version_id'])
            return None

    async def _find_best_previous_version(self, exclude_version: int) -> Optional[int]:
        """Find the best performing previous version to rollback to."""
        async with self.db.acquire() as conn:
            # Find version with best hourly P&L that wasn't reverted
            row = await conn.fetchrow("""
                SELECT pv.version_id, pp.hourly_pnl_avg
                FROM parameter_versions pv
                JOIN parameter_performance pp ON pp.version_id = pv.version_id
                WHERE pv.version_id < $1
                  AND pv.reverted_at IS NULL
                  AND pp.hours_active >= $2
                ORDER BY pp.hourly_pnl_avg DESC
                LIMIT 1
            """, exclude_version, self.min_monitoring_hours)

            return row['version_id'] if row else None

    async def get_current_health(self) -> dict:
        """
        Get current health status for dashboard.

        Returns:
            Dict with health metrics and status
        """
        current_version = self.config_manager.current_version
        perf = await self._get_version_performance(current_version)

        if not perf:
            return {
                "status": "unknown",
                "message": "No performance data",
                "version": current_version,
                "metrics": {}
            }

        # Determine status
        issues = []

        if perf['hours_active'] < self.min_monitoring_hours:
            status = "monitoring"
            message = f"Monitoring new parameters ({perf['hours_active']}h/{self.min_monitoring_hours}h)"
        elif perf['trades'] < self.min_trades_for_evaluation:
            status = "monitoring"
            message = f"Waiting for trades ({perf['trades']}/{self.min_trades_for_evaluation})"
        else:
            if perf['win_rate'] < self.min_win_rate:
                issues.append(f"Low win rate: {perf['win_rate']:.1f}%")
            if perf['max_drawdown'] > self.max_drawdown_pct:
                issues.append(f"High drawdown: {perf['max_drawdown']:.1f}%")
            if perf['hourly_pnl'] < 0:
                issues.append(f"Negative hourly P&L: ${perf['hourly_pnl']:.4f}")

            if issues:
                status = "warning"
                message = "; ".join(issues)
            else:
                status = "healthy"
                message = f"Performing well: ${perf['hourly_pnl']:.4f}/hr, {perf['win_rate']:.0f}% WR"

        return {
            "status": status,
            "message": message,
            "version": current_version,
            "metrics": perf
        }
