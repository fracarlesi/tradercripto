"""
HLQuantBot Learning Module Service
===================================

Continuous optimization service that learns from trading performance.

Features:
- Aggregates metrics per strategy (win rate, PnL, Sharpe ratio, drawdown)
- LLM-driven parameter optimization suggestions
- Gradual parameter adjustment with rollback protection
- Scheduled optimization cycles (hourly, 4-hourly, daily)

Usage:
    from simple_bot.services import LearningModuleService, create_learning_module

    service = create_learning_module(bus=bus, db=db, llm=llm, config=config)
    await service.start()

Author: Francesco Carlesi
"""

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseService
from .message_bus import Message, MessageBus, Topic

# Try to import Database and LLM client
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from database.db import Database
    DB_AVAILABLE = True
except ImportError:
    Database = None  # type: ignore
    DB_AVAILABLE = False

try:
    from simple_bot.llm.client import DeepSeekClient
    from simple_bot.llm.prompts import OPTIMIZATION_PROMPT
    LLM_AVAILABLE = True
except ImportError:
    DeepSeekClient = None  # type: ignore
    OPTIMIZATION_PROMPT = ""  # type: ignore
    LLM_AVAILABLE = False


logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class StrategyMetrics:
    """
    Aggregated performance metrics for a strategy.
    
    Attributes:
        strategy: Strategy name (momentum, mean_reversion, breakout, funding_arb)
        trades: Total number of closed trades
        wins: Number of winning trades
        losses: Number of losing trades
        total_pnl: Total profit/loss
        avg_pnl: Average profit/loss per trade
        sharpe_ratio: Risk-adjusted return (annualized)
        max_drawdown: Maximum peak-to-trough decline (percentage)
        avg_duration_minutes: Average trade duration
        avg_slippage: Average execution slippage (percentage)
        fill_rate: Percentage of orders successfully filled
        period_hours: Time period these metrics cover
    """
    
    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    avg_duration_minutes: float = 0.0
    avg_slippage: float = 0.0
    fill_rate: float = 100.0
    period_hours: int = 24
    
    @property
    def win_rate(self) -> float:
        """Calculate win rate as percentage."""
        if self.trades == 0:
            return 0.0
        return (self.wins / self.trades) * 100
    
    @property
    def profit_factor(self) -> float:
        """Calculate profit factor (gross profit / gross loss)."""
        if self.losses == 0:
            return float("inf") if self.wins > 0 else 0.0
        return self.wins / self.losses if self.losses > 0 else 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "strategy": self.strategy,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 2),
            "total_pnl": float(self.total_pnl),
            "avg_pnl": float(self.avg_pnl),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "max_drawdown": round(self.max_drawdown, 2),
            "avg_duration_minutes": round(self.avg_duration_minutes, 1),
            "avg_slippage": round(self.avg_slippage, 4),
            "fill_rate": round(self.fill_rate, 2),
            "period_hours": self.period_hours,
        }


@dataclass
class OptimizationResult:
    """
    Result of an LLM-driven optimization cycle.
    
    Attributes:
        timestamp: When the optimization was performed
        strategy: Target strategy
        adjustments: Parameter adjustment suggestions
        confidence: LLM confidence in suggestions (0-1)
        expected_improvement: Expected outcome description
        risks: Identified risks
        applied: Whether adjustments were applied
        rollback_reason: Reason if rolled back
    """
    
    timestamp: datetime
    strategy: str
    adjustments: Dict[str, Dict[str, Any]]
    confidence: float
    expected_improvement: str
    risks: List[str] = field(default_factory=list)
    applied: bool = False
    rollback_reason: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "strategy": self.strategy,
            "adjustments": self.adjustments,
            "confidence": self.confidence,
            "expected_improvement": self.expected_improvement,
            "risks": self.risks,
            "applied": self.applied,
            "rollback_reason": self.rollback_reason,
        }


class OptimizationCycle(str, Enum):
    """Optimization cycle types."""
    
    HOURLY = "hourly"         # Collect metrics
    FOUR_HOURLY = "4h"        # LLM optimization
    DAILY = "daily"           # Full strategy review
    WEEKLY = "weekly"         # Backtest if enough data
    
    def __str__(self) -> str:
        return self.value


# =============================================================================
# Learning Module Service
# =============================================================================

class LearningModuleService(BaseService):
    """
    Service that optimizes trading parameters over time.
    
    The Learning Module:
    1. Collects performance metrics from trades and fills
    2. Aggregates metrics per strategy
    3. Periodically calls LLM for optimization suggestions
    4. Publishes config updates for gradual parameter adjustment
    5. Tracks optimization history and can rollback poor changes
    
    Optimization cycles:
    - Hourly: Collect and aggregate metrics
    - 4-Hourly: LLM-driven parameter suggestions
    - Daily: Full strategy performance review
    - Weekly: Backtest parameter combinations (if enough data)
    
    Example:
        service = LearningModuleService(
            name="learning_module",
            bus=bus,
            db=db,
            llm=llm,
            config=config
        )
        await service.start()
    """
    
    def __init__(
        self,
        name: str = "learning_module",
        bus: Optional[MessageBus] = None,
        db: Optional["Database"] = None,
        llm: Optional["DeepSeekClient"] = None,
        config: Optional[Dict[str, Any]] = None,
        config_path: Optional[str] = None,
    ) -> None:
        """
        Initialize Learning Module.
        
        Args:
            name: Service name
            bus: MessageBus for pub/sub
            db: Database for metrics persistence
            llm: DeepSeekClient for LLM optimization
            config: Configuration dict (learning_module section)
            config_path: Path to YAML config
        """
        super().__init__(
            name=name,
            bus=bus,
            db=db,
            config=config,
            config_path=config_path,
            loop_interval_seconds=60,  # Check every minute
        )
        
        self.llm = llm
        
        # Configuration from learning_module section
        lm_config = self._config.get("learning_module", {})
        self.enabled = lm_config.get("enabled", True)
        self.optimization_interval_hours = lm_config.get("optimization_interval_hours", 4)
        self.min_trades_for_optimization = lm_config.get("min_trades_for_optimization", 20)
        self.performance_window_hours = lm_config.get("performance_window_hours", 24)
        self.rollback_threshold_pct = lm_config.get("rollback_threshold_pct", -5.0)
        
        # Strategy configs for optimization context
        self.strategies_config = self._config.get("strategies", {})
        
        # State
        self._metrics_cache: Dict[str, StrategyMetrics] = {}
        self._optimization_history: List[OptimizationResult] = []
        self._last_optimization: Optional[datetime] = None
        self._last_hourly_collection: Optional[datetime] = None
        self._pending_fills: List[Dict[str, Any]] = []
        self._pending_metrics: List[Dict[str, Any]] = []
        
        # Cycle tasks
        self._hourly_task: Optional[asyncio.Task] = None
        self._four_hourly_task: Optional[asyncio.Task] = None
        self._daily_task: Optional[asyncio.Task] = None
        
        self._logger.info(
            "LearningModuleService initialized (opt_interval=%dh, min_trades=%d)",
            self.optimization_interval_hours,
            self.min_trades_for_optimization,
        )
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def _on_start(self) -> None:
        """Initialize subscriptions and start optimization cycles."""
        if not self.enabled:
            self._logger.warning("Learning Module is disabled in config")
            return
        
        # Subscribe to relevant topics
        await self.subscribe(Topic.FILLS, self._on_fill)
        await self.subscribe(Topic.METRICS, self._on_metrics)
        
        # Start optimization cycle tasks
        self._hourly_task = asyncio.create_task(
            self._hourly_cycle(),
            name="learning_hourly"
        )
        
        self._four_hourly_task = asyncio.create_task(
            self._four_hourly_cycle(),
            name="learning_4h"
        )
        
        self._daily_task = asyncio.create_task(
            self._daily_cycle(),
            name="learning_daily"
        )
        
        self._logger.info("Learning Module started with optimization cycles")
    
    async def _on_stop(self) -> None:
        """Clean up cycle tasks."""
        # Cancel cycle tasks
        for task in [self._hourly_task, self._four_hourly_task, self._daily_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self._hourly_task = None
        self._four_hourly_task = None
        self._daily_task = None
        
        self._logger.info("Learning Module stopped")
    
    async def _run_iteration(self) -> None:
        """Main loop iteration - process pending data."""
        if not self.enabled:
            return
        
        # Process any accumulated pending fills
        if self._pending_fills:
            await self._process_pending_fills()
        
        # Process any accumulated pending metrics
        if self._pending_metrics:
            await self._process_pending_metrics()
    
    async def _health_check_impl(self) -> bool:
        """Custom health check."""
        if not self.enabled:
            return True
        
        # Check if cycles are running
        cycles_ok = all([
            self._hourly_task and not self._hourly_task.done(),
            self._four_hourly_task and not self._four_hourly_task.done(),
            self._daily_task and not self._daily_task.done(),
        ])
        
        return cycles_ok
    
    # =========================================================================
    # Message Handlers
    # =========================================================================
    
    async def _on_fill(self, msg: Message) -> None:
        """Handle fill messages from execution engine."""
        fill = msg.payload
        if isinstance(fill, dict):
            self._pending_fills.append(fill)
            self._logger.debug("Received fill for %s", fill.get("symbol", "unknown"))
    
    async def _on_metrics(self, msg: Message) -> None:
        """Handle metrics messages from other services."""
        metrics = msg.payload
        if isinstance(metrics, dict):
            self._pending_metrics.append(metrics)
            self._logger.debug("Received metrics from %s", msg.source)
    
    async def _process_pending_fills(self) -> None:
        """Process accumulated fill data."""
        fills_to_process = self._pending_fills.copy()
        self._pending_fills.clear()
        
        # Group fills by strategy
        strategy_fills: Dict[str, List[Dict]] = {}
        for fill in fills_to_process:
            strategy = fill.get("strategy", "unknown")
            if strategy not in strategy_fills:
                strategy_fills[strategy] = []
            strategy_fills[strategy].append(fill)
        
        self._logger.debug(
            "Processed %d fills across %d strategies",
            len(fills_to_process),
            len(strategy_fills)
        )
    
    async def _process_pending_metrics(self) -> None:
        """Process accumulated metrics data."""
        metrics_to_process = self._pending_metrics.copy()
        self._pending_metrics.clear()
        
        # Aggregate metrics by source/type
        for metrics in metrics_to_process:
            source = metrics.get("source", "unknown")
            self._logger.debug("Processing metrics from %s", source)
    
    # =========================================================================
    # Optimization Cycles
    # =========================================================================
    
    async def _hourly_cycle(self) -> None:
        """Hourly metrics collection cycle."""
        # Wait for initial alignment
        await self._wait_for_next_hour()
        
        while True:
            try:
                self._logger.info("Starting hourly metrics collection")
                metrics = await self._collect_metrics()
                
                # Update cache
                for strategy, strat_metrics in metrics.items():
                    self._metrics_cache[strategy] = strat_metrics
                
                self._last_hourly_collection = datetime.now(timezone.utc)
                
                # Publish metrics update
                await self.publish(Topic.METRICS, {
                    "type": "strategy_metrics",
                    "metrics": {k: v.to_dict() for k, v in metrics.items()},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                
                self._logger.info(
                    "Collected metrics for %d strategies",
                    len(metrics)
                )
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.error("Hourly cycle error: %s", e, exc_info=True)
            
            # Wait for next hour
            await asyncio.sleep(3600)  # 1 hour
    
    async def _four_hourly_cycle(self) -> None:
        """4-hourly LLM optimization cycle."""
        # Wait for initial offset (don't run at same time as hourly)
        await asyncio.sleep(300)  # 5 minute offset
        
        while True:
            try:
                await asyncio.sleep(self.optimization_interval_hours * 3600)
                
                self._logger.info("Starting 4-hourly optimization cycle")
                
                # Collect fresh metrics
                metrics = await self._collect_metrics()
                
                # Run optimization for each strategy with enough data
                for strategy, strat_metrics in metrics.items():
                    if strat_metrics.trades >= self.min_trades_for_optimization:
                        await self._optimize_strategy(strategy, strat_metrics)
                    else:
                        self._logger.debug(
                            "Skipping optimization for %s: only %d trades (min=%d)",
                            strategy,
                            strat_metrics.trades,
                            self.min_trades_for_optimization,
                        )
                
                self._last_optimization = datetime.now(timezone.utc)
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.error("4-hourly cycle error: %s", e, exc_info=True)
    
    async def _daily_cycle(self) -> None:
        """Daily full review cycle."""
        # Wait for initial offset (run at midnight-ish)
        await self._wait_for_next_day()
        
        while True:
            try:
                self._logger.info("Starting daily review cycle")
                
                # Full performance review
                metrics = await self._collect_metrics(hours=24)
                
                # Check for strategies needing intervention
                for strategy, strat_metrics in metrics.items():
                    await self._daily_strategy_review(strategy, strat_metrics)
                
                # Check for rollback conditions
                await self._check_rollback_conditions()
                
                self._logger.info("Daily review complete")
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.error("Daily cycle error: %s", e, exc_info=True)
            
            # Wait for next day
            await asyncio.sleep(86400)  # 24 hours
    
    async def _wait_for_next_hour(self) -> None:
        """Wait until the next hour boundary."""
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait_seconds = (next_hour - now).total_seconds()
        
        self._logger.debug("Waiting %.0f seconds for next hour", wait_seconds)
        await asyncio.sleep(max(1, wait_seconds))
    
    async def _wait_for_next_day(self) -> None:
        """Wait until the next day boundary (midnight UTC)."""
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0  # 00:05 to avoid conflicts
        )
        wait_seconds = (tomorrow - now).total_seconds()
        
        self._logger.debug("Waiting %.0f seconds for daily cycle", wait_seconds)
        await asyncio.sleep(max(1, wait_seconds))
    
    # =========================================================================
    # Metrics Collection
    # =========================================================================
    
    async def _collect_metrics(
        self,
        hours: Optional[int] = None
    ) -> Dict[str, StrategyMetrics]:
        """
        Collect and aggregate metrics from database.
        
        Args:
            hours: Time window in hours (default: performance_window_hours)
        
        Returns:
            Dict mapping strategy name to StrategyMetrics
        """
        if not self.db:
            self._logger.warning("No database available for metrics collection")
            return {}
        
        hours = hours or self.performance_window_hours
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        # Get closed trades from the period
        try:
            trades = await self.db.get_trades(is_closed=True, limit=1000)
            
            # Filter by time and group by strategy
            strategy_trades: Dict[str, List[Dict]] = {}
            
            for trade in trades:
                exit_time = trade.get("exit_time")
                if exit_time and exit_time >= since:
                    strategy = trade.get("strategy", "unknown")
                    if strategy not in strategy_trades:
                        strategy_trades[strategy] = []
                    strategy_trades[strategy].append(trade)
            
            # Calculate metrics for each strategy
            metrics: Dict[str, StrategyMetrics] = {}
            
            for strategy, strat_trades in strategy_trades.items():
                metrics[strategy] = self._calculate_strategy_metrics(
                    strategy, strat_trades, hours
                )
            
            return metrics
            
        except Exception as e:
            self._logger.error("Failed to collect metrics: %s", e)
            return {}
    
    def _calculate_strategy_metrics(
        self,
        strategy: str,
        trades: List[Dict],
        hours: int
    ) -> StrategyMetrics:
        """
        Calculate aggregated metrics for a strategy.
        
        Args:
            strategy: Strategy name
            trades: List of trade dicts
            hours: Time period
        
        Returns:
            StrategyMetrics instance
        """
        if not trades:
            return StrategyMetrics(strategy=strategy, period_hours=hours)
        
        # Basic counts
        total = len(trades)
        wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
        losses = total - wins
        
        # PnL calculations
        pnls = [Decimal(str(t.get("net_pnl", 0))) for t in trades]
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / total if total > 0 else Decimal("0")
        
        # Sharpe ratio (simplified - assumes daily frequency)
        if len(pnls) > 1:
            pnl_floats = [float(p) for p in pnls]
            mean_pnl = sum(pnl_floats) / len(pnl_floats)
            variance = sum((p - mean_pnl) ** 2 for p in pnl_floats) / len(pnl_floats)
            std_dev = math.sqrt(variance) if variance > 0 else 0.001
            sharpe = (mean_pnl / std_dev) * math.sqrt(252)  # Annualized
        else:
            sharpe = 0.0
        
        # Drawdown calculation
        cumulative = []
        running_total = 0.0
        for pnl in pnls:
            running_total += float(pnl)
            cumulative.append(running_total)
        
        max_drawdown = 0.0
        peak = cumulative[0] if cumulative else 0.0
        for value in cumulative:
            if value > peak:
                peak = value
            drawdown = (peak - value) / abs(peak) * 100 if peak != 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
        
        # Duration calculation
        durations = []
        for trade in trades:
            duration_sec = trade.get("duration_seconds", 0)
            if duration_sec:
                durations.append(duration_sec / 60)  # Convert to minutes
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        
        return StrategyMetrics(
            strategy=strategy,
            trades=total,
            wins=wins,
            losses=losses,
            total_pnl=total_pnl,
            avg_pnl=avg_pnl,
            sharpe_ratio=sharpe,
            max_drawdown=max_drawdown,
            avg_duration_minutes=avg_duration,
            avg_slippage=0.0,  # TODO: Calculate from fills
            fill_rate=100.0,   # TODO: Calculate from orders
            period_hours=hours,
        )
    
    # =========================================================================
    # LLM Optimization
    # =========================================================================
    
    async def _optimize_strategy(
        self,
        strategy: str,
        metrics: StrategyMetrics
    ) -> Optional[OptimizationResult]:
        """
        Call LLM to get optimization suggestions for a strategy.
        
        Args:
            strategy: Strategy name
            metrics: Current strategy metrics
        
        Returns:
            OptimizationResult if successful, None otherwise
        """
        if not self.llm or not LLM_AVAILABLE:
            self._logger.warning("LLM not available for optimization")
            return None
        
        if not self.llm.is_available:
            self._logger.debug("LLM rate limit reached, skipping optimization")
            return None
        
        # Get current strategy parameters
        strategy_config = self.strategies_config.get(strategy, {})
        if not strategy_config:
            self._logger.warning("No config found for strategy: %s", strategy)
            return None
        
        # Format current parameters for prompt
        current_params = "\n".join([
            f"- {k}: {v}" for k, v in strategy_config.items()
            if k not in ("enabled", "weight")
        ])
        
        # Format trade details
        trade_details = f"""
- Win Rate: {metrics.win_rate:.1f}%
- Profit Factor: {metrics.profit_factor:.2f}
- Max Consecutive Losses: N/A (requires more data)
"""
        
        # Prepare context for OPTIMIZATION_PROMPT
        context = {
            "strategy": strategy,
            "current_params": current_params,
            "hours": metrics.period_hours,
            "total_trades": metrics.trades,
            "win_rate": metrics.win_rate,
            "net_pnl": float(metrics.total_pnl),
            "avg_duration": metrics.avg_duration_minutes,
            "max_drawdown": metrics.max_drawdown,
            "trade_details": trade_details,
            "avg_adx": 25.0,       # TODO: Get from market data
            "avg_volatility": 2.5, # TODO: Get from market data
            "market_regime": "neutral",  # TODO: Get from regime detector
        }
        
        try:
            # Format prompt
            prompt = OPTIMIZATION_PROMPT.format(**context)
            
            # Call LLM
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an expert quantitative trading optimizer. "
                        "Analyze the performance data and suggest parameter adjustments. "
                        "Be conservative - only suggest changes if you're confident. "
                        "Always respond with valid JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            
            response = await self.llm.chat(
                messages,
                response_format={"type": "json_object"},
            )
            
            # Parse response
            result = self._parse_optimization_response(strategy, response)
            
            if result:
                self._optimization_history.append(result)
                
                # Apply if confidence is high enough
                if result.confidence >= 0.7 and result.adjustments:
                    await self._apply_optimizations(result)
                    result.applied = True
                    self._logger.info(
                        "Applied optimization for %s with %.0f%% confidence",
                        strategy,
                        result.confidence * 100
                    )
                else:
                    self._logger.info(
                        "Skipped optimization for %s (confidence=%.0f%%)",
                        strategy,
                        result.confidence * 100
                    )
            
            return result
            
        except Exception as e:
            self._logger.error("Optimization failed for %s: %s", strategy, e)
            return None
    
    def _parse_optimization_response(
        self,
        strategy: str,
        response: str
    ) -> Optional[OptimizationResult]:
        """
        Parse LLM optimization response.
        
        Args:
            strategy: Strategy name
            response: Raw LLM response
        
        Returns:
            OptimizationResult if parsing succeeds
        """
        try:
            # Clean up response
            text = response.strip()
            
            # Remove markdown code blocks if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:-1] if lines[-1].startswith("```") else lines[1:]
                text = "\n".join(lines)
            
            # Parse JSON
            data = json.loads(text)
            
            return OptimizationResult(
                timestamp=datetime.now(timezone.utc),
                strategy=strategy,
                adjustments=data.get("adjustments", {}),
                confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
                expected_improvement=data.get("expected_improvement", ""),
                risks=data.get("risks", []),
            )
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self._logger.error("Failed to parse optimization response: %s", e)
            return None
    
    async def _apply_optimizations(
        self,
        result: OptimizationResult
    ) -> None:
        """
        Apply optimization suggestions by publishing config updates.
        
        Args:
            result: OptimizationResult with adjustments
        """
        if not result.adjustments:
            return
        
        # Validate and prepare updates
        updates: Dict[str, Any] = {}
        
        for param_name, adjustment in result.adjustments.items():
            current = adjustment.get("current")
            suggested = adjustment.get("suggested")
            
            if current is None or suggested is None:
                continue
            
            # Limit adjustment magnitude (max 20% change)
            if isinstance(current, (int, float)) and isinstance(suggested, (int, float)):
                max_change = abs(current * 0.2)
                if abs(suggested - current) > max_change:
                    # Clamp to max change
                    if suggested > current:
                        suggested = current + max_change
                    else:
                        suggested = current - max_change
                    
                    self._logger.debug(
                        "Clamped %s adjustment from %.4f to %.4f",
                        param_name,
                        adjustment.get("suggested"),
                        suggested
                    )
            
            updates[param_name] = suggested
        
        if updates:
            # Publish config update
            await self.publish(Topic.CONFIG_UPDATES, {
                "type": "strategy_optimization",
                "strategy": result.strategy,
                "updates": updates,
                "confidence": result.confidence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": result.expected_improvement,
            })
            
            self._logger.info(
                "Published %d parameter updates for %s",
                len(updates),
                result.strategy
            )
    
    # =========================================================================
    # Daily Review & Rollback
    # =========================================================================
    
    async def _daily_strategy_review(
        self,
        strategy: str,
        metrics: StrategyMetrics
    ) -> None:
        """
        Perform daily review of a strategy's performance.
        
        Args:
            strategy: Strategy name
            metrics: 24-hour metrics
        """
        self._logger.info(
            "Daily review for %s: trades=%d, win_rate=%.1f%%, pnl=$%.2f",
            strategy,
            metrics.trades,
            metrics.win_rate,
            float(metrics.total_pnl)
        )
        
        # Check for concerning patterns
        warnings = []
        
        if metrics.win_rate < 40 and metrics.trades >= 5:
            warnings.append(f"Low win rate: {metrics.win_rate:.1f}%")
        
        if metrics.max_drawdown > 10:
            warnings.append(f"High drawdown: {metrics.max_drawdown:.1f}%")
        
        if float(metrics.total_pnl) < self.rollback_threshold_pct:
            warnings.append(f"PnL below threshold: ${float(metrics.total_pnl):.2f}")
        
        if warnings:
            self._logger.warning(
                "Strategy %s has warnings: %s",
                strategy,
                ", ".join(warnings)
            )
            
            # Publish warning
            await self.publish(Topic.METRICS, {
                "type": "strategy_warning",
                "strategy": strategy,
                "warnings": warnings,
                "metrics": metrics.to_dict(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    
    async def _check_rollback_conditions(self) -> None:
        """Check if any recent optimizations should be rolled back."""
        if not self._optimization_history:
            return
        
        # Check optimizations from the last 24 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            opt for opt in self._optimization_history
            if opt.applied and opt.timestamp >= cutoff and not opt.rollback_reason
        ]
        
        for optimization in recent:
            # Get current metrics for the strategy
            if optimization.strategy in self._metrics_cache:
                metrics = self._metrics_cache[optimization.strategy]
                
                # Check if performance degraded significantly
                if float(metrics.total_pnl) < self.rollback_threshold_pct:
                    self._logger.warning(
                        "Rolling back optimization for %s: PnL=%.2f (threshold=%.2f)",
                        optimization.strategy,
                        float(metrics.total_pnl),
                        self.rollback_threshold_pct
                    )
                    
                    optimization.rollback_reason = (
                        f"Performance below threshold: PnL=${float(metrics.total_pnl):.2f}"
                    )
                    
                    # Publish rollback request
                    await self._request_rollback(optimization)
    
    async def _request_rollback(self, optimization: OptimizationResult) -> None:
        """
        Request rollback of an optimization.
        
        Args:
            optimization: The optimization to roll back
        """
        # Reverse the adjustments
        rollback_updates: Dict[str, Any] = {}
        
        for param_name, adjustment in optimization.adjustments.items():
            current = adjustment.get("current")
            if current is not None:
                rollback_updates[param_name] = current
        
        if rollback_updates:
            await self.publish(Topic.CONFIG_UPDATES, {
                "type": "strategy_rollback",
                "strategy": optimization.strategy,
                "updates": rollback_updates,
                "reason": optimization.rollback_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            
            self._logger.info(
                "Published rollback for %s: %s",
                optimization.strategy,
                optimization.rollback_reason
            )
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get current metrics cache."""
        return {k: v.to_dict() for k, v in self._metrics_cache.items()}
    
    def get_optimization_history(
        self,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get recent optimization history."""
        recent = self._optimization_history[-limit:]
        return [opt.to_dict() for opt in reversed(recent)]
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        base_stats = super().stats
        base_stats.update({
            "enabled": self.enabled,
            "optimization_interval_hours": self.optimization_interval_hours,
            "min_trades": self.min_trades_for_optimization,
            "strategies_tracked": len(self._metrics_cache),
            "optimizations_performed": len(self._optimization_history),
            "last_optimization": (
                self._last_optimization.isoformat()
                if self._last_optimization else None
            ),
            "last_hourly": (
                self._last_hourly_collection.isoformat()
                if self._last_hourly_collection else None
            ),
        })
        return base_stats


# =============================================================================
# Factory Function
# =============================================================================

def create_learning_module(
    bus: Optional[MessageBus] = None,
    db: Optional["Database"] = None,
    llm: Optional["DeepSeekClient"] = None,
    config: Optional[Dict[str, Any]] = None,
) -> LearningModuleService:
    """
    Create a LearningModuleService instance.
    
    Args:
        bus: MessageBus for pub/sub
        db: Database for persistence
        llm: DeepSeekClient for LLM optimization
        config: Full bot configuration dict
    
    Returns:
        Configured LearningModuleService
    """
    return LearningModuleService(
        name="learning_module",
        bus=bus,
        db=db,
        llm=llm,
        config=config,
    )
