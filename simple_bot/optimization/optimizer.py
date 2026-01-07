"""
Main Optimization Orchestrator
Coordinates the hourly optimization cycle.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
import logging

from .data_collector import HourlyMetricsCollector
from .summarizer import TieredSummarizer
from .deepseek_client import DeepSeekOptimizer
from .config_manager import HotReloadConfigManager
from .rollback import SafetyMonitor

logger = logging.getLogger(__name__)


class OptimizationOrchestrator:
    """
    Orchestrates the complete optimization cycle:
    1. Collect hourly metrics
    2. Build summarized context
    3. Call DeepSeek for recommendations
    4. Apply changes with safety checks
    5. Monitor and rollback if needed
    """

    def __init__(
        self,
        db,
        info_client,
        config_manager: HotReloadConfigManager,
        logger_instance: logging.Logger = None,
        min_confidence: float = 0.6,
        min_hours_between_optimizations: int = 1
    ):
        """
        Initialize orchestrator.

        Args:
            db: Database connection pool
            info_client: Hyperliquid info client
            config_manager: HotReloadConfigManager instance
            logger_instance: Optional logger
            min_confidence: Minimum LLM confidence to apply changes
            min_hours_between_optimizations: Cooldown between optimizations
        """
        self.db = db
        self.log = logger_instance or logger
        self.config_manager = config_manager
        self.min_confidence = min_confidence
        self.min_hours_between_optimizations = min_hours_between_optimizations

        # Components
        self.collector = HourlyMetricsCollector(db, info_client)
        self.summarizer = TieredSummarizer(db)
        self.optimizer = DeepSeekOptimizer()
        self.safety = SafetyMonitor(db, config_manager, self.log)

        # State
        self.running = False
        self.last_optimization: Optional[datetime] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the optimization loop in background."""
        if self.running:
            self.log.warning("Optimization orchestrator already running")
            return

        self.running = True
        self.log.info("[OPTIMIZER] Starting optimization service")

        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        """Stop the optimization loop."""
        self.running = False
        self.log.info("[OPTIMIZER] Stopping optimization service")

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self.optimizer.close()

    async def _run_loop(self):
        """Main optimization loop."""
        while self.running:
            try:
                # Wait until next hour boundary
                await self._sleep_until_next_hour()

                if not self.running:
                    break

                await self._run_cycle()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"[OPTIMIZER] Error in cycle: {e}", exc_info=True)
                # Continue running, retry next hour

    async def _run_cycle(self):
        """Run one optimization cycle."""
        now = datetime.now(timezone.utc)
        self.log.info(f"[OPTIMIZER] Starting optimization cycle at {now}")

        # 1. Collect metrics for the completed hour
        self.log.info("[OPTIMIZER] Collecting hourly metrics...")
        metrics = await self.collector.collect_hourly_metrics(
            self.config_manager.current_version
        )
        self.log.info(
            f"[OPTIMIZER] Collected: {metrics['trades_count']} trades, "
            f"${float(metrics['net_pnl']):.2f} P&L"
        )

        # 2. Check safety first - rollback if needed
        rolled_back, reason = await self.safety.check_and_rollback()
        if rolled_back:
            self.log.warning(f"[OPTIMIZER] Rolled back due to: {reason}")
            # Don't optimize this cycle - let the rolled-back params run
            return

        # 3. Check cooldown
        if self.last_optimization:
            hours_since = (now - self.last_optimization).total_seconds() / 3600
            if hours_since < self.min_hours_between_optimizations:
                self.log.info(
                    f"[OPTIMIZER] Skipping - cooldown "
                    f"({hours_since:.1f}h < {self.min_hours_between_optimizations}h)"
                )
                return

        # 4. Build context for LLM
        self.log.info("[OPTIMIZER] Building context for LLM...")
        current_params = self.config_manager.current_config
        context = await self.summarizer.get_context_for_llm(current_params)
        context_str = self.summarizer.format_for_prompt(context)

        # Estimate tokens
        token_estimate = await self.summarizer.get_token_estimate(context)
        self.log.info(f"[OPTIMIZER] Context prepared (~{token_estimate} tokens)")

        # 5. Call DeepSeek
        self.log.info("[OPTIMIZER] Calling DeepSeek Reasoner...")
        result = await self.optimizer.optimize_parameters(context_str, current_params)

        # 6. Log the optimization run
        run_id = await self._log_optimization_run(context, result)

        if result.action == "error":
            self.log.error(f"[OPTIMIZER] DeepSeek error: {result.reasoning}")
            await self._update_run_status(run_id, "failed", result.reasoning)
            return

        if result.action == "no_change":
            self.log.info(f"[OPTIMIZER] No changes suggested: {result.reasoning}")
            await self._update_run_status(run_id, "skipped")
            return

        if not result.success:
            self.log.warning(f"[OPTIMIZER] Optimization failed: {result.reasoning}")
            await self._update_run_status(run_id, "failed", result.reasoning)
            return

        # 7. Check confidence threshold
        if result.confidence < self.min_confidence:
            self.log.info(
                f"[OPTIMIZER] Confidence too low ({result.confidence:.2f} < {self.min_confidence}), "
                "skipping changes"
            )
            await self._update_run_status(run_id, "skipped", "Low confidence")
            return

        # 8. Constrain parameter changes to max +/-10% per cycle
        self.log.info(
            f"[OPTIMIZER] Applying new parameters (confidence: {result.confidence:.2f})"
        )
        self.log.info(f"[OPTIMIZER] Reasoning: {result.reasoning}")

        # Apply change limits to prevent large swings
        constrained_params = self._constrain_parameter_changes(
            current_params, 
            result.new_params,
            max_change_pct=0.10  # 10% max change per cycle
        )

        # Log what's changing (after constraints)
        self._log_param_changes(current_params, constrained_params)

        version_id = await self.config_manager.apply_new_config(
            constrained_params,
            source="llm",
            reasoning=result.reasoning + " [Changes constrained to +/-10%]"
        )

        # Update run record with applied version
        await self._update_run_status(run_id, "success", applied_version=version_id)

        self.last_optimization = now
        self.log.info(f"[OPTIMIZER] Applied parameter version {version_id}")

    def _log_param_changes(self, old: dict, new: dict):
        """Log parameter changes for visibility."""
        changes = []

        # Global params
        for key in ['tp_pct', 'sl_pct', 'position_size_usd', 'leverage']:
            if old.get(key) != new.get(key):
                changes.append(f"{key}: {old.get(key)} -> {new.get(key)}")

        # Strategy params
        for strategy in ['momentum', 'mean_reversion', 'breakout']:
            old_s = old.get(strategy, {})
            new_s = new.get(strategy, {})
            for key in new_s:
                if old_s.get(key) != new_s.get(key):
                    changes.append(f"{strategy}.{key}: {old_s.get(key)} -> {new_s.get(key)}")

        if changes:
            self.log.info(f"[OPTIMIZER] Changes: {', '.join(changes)}")
        else:
            self.log.info("[OPTIMIZER] No actual parameter changes")

    def _constrain_parameter_changes(
        self, 
        current_params: Dict, 
        new_params: Dict, 
        max_change_pct: float = 0.10
    ) -> Dict:
        """
        Constrain parameter changes to prevent large swings.
        
        Each numeric parameter can only change by max_change_pct (default 10%)
        per optimization cycle. This prevents overfitting and ensures
        gradual adaptation.
        
        Args:
            current_params: Current parameter configuration
            new_params: Proposed new parameters from LLM
            max_change_pct: Maximum allowed change per cycle (0.10 = 10%)
            
        Returns:
            Constrained parameters dict
        """
        constrained = {}
        
        # Helper function to constrain a single value
        def constrain_value(current: float, proposed: float, param_name: str) -> float:
            if current == 0:
                return proposed  # Can't constrain from zero
            
            max_increase = current * (1 + max_change_pct)
            max_decrease = current * (1 - max_change_pct)
            
            original = proposed
            constrained_val = max(min(proposed, max_increase), max_decrease)
            
            if constrained_val != original:
                self.log.info(
                    f"[OPTIMIZER] Constrained {param_name}: "
                    f"{original:.4f} -> {constrained_val:.4f} "
                    f"(max change {max_change_pct*100:.0f}%)"
                )
            
            return constrained_val
        
        # Constrain global parameters
        constrained["tp_pct"] = constrain_value(
            current_params["tp_pct"], 
            new_params["tp_pct"], 
            "tp_pct"
        )
        constrained["sl_pct"] = constrain_value(
            current_params["sl_pct"], 
            new_params["sl_pct"], 
            "sl_pct"
        )
        constrained["position_size_usd"] = constrain_value(
            current_params["position_size_usd"], 
            new_params["position_size_usd"], 
            "position_size_usd"
        )
        # Leverage is an integer, round after constraining
        constrained["leverage"] = int(round(constrain_value(
            current_params["leverage"], 
            new_params["leverage"], 
            "leverage"
        )))
        
        # Constrain momentum parameters
        curr_m = current_params["momentum"]
        new_m = new_params["momentum"]
        constrained["momentum"] = {
            "enabled": new_m["enabled"],  # Boolean, no constraint
            "ema_fast": int(round(constrain_value(
                curr_m["ema_fast"], new_m["ema_fast"], "momentum.ema_fast"
            ))),
            "ema_slow": int(round(constrain_value(
                curr_m["ema_slow"], new_m["ema_slow"], "momentum.ema_slow"
            ))),
            "rsi_period": int(round(constrain_value(
                curr_m["rsi_period"], new_m["rsi_period"], "momentum.rsi_period"
            ))),
            "rsi_long_threshold": int(round(constrain_value(
                curr_m["rsi_long_threshold"], new_m["rsi_long_threshold"], "momentum.rsi_long_threshold"
            ))),
            "rsi_short_threshold": int(round(constrain_value(
                curr_m["rsi_short_threshold"], new_m["rsi_short_threshold"], "momentum.rsi_short_threshold"
            ))),
        }
        
        # Constrain mean reversion parameters
        curr_mr = current_params["mean_reversion"]
        new_mr = new_params["mean_reversion"]
        constrained["mean_reversion"] = {
            "enabled": new_mr["enabled"],
            "rsi_oversold": int(round(constrain_value(
                curr_mr["rsi_oversold"], new_mr["rsi_oversold"], "mean_reversion.rsi_oversold"
            ))),
            "rsi_overbought": int(round(constrain_value(
                curr_mr["rsi_overbought"], new_mr["rsi_overbought"], "mean_reversion.rsi_overbought"
            ))),
            "bb_period": int(round(constrain_value(
                curr_mr["bb_period"], new_mr["bb_period"], "mean_reversion.bb_period"
            ))),
            "bb_std": constrain_value(
                curr_mr["bb_std"], new_mr["bb_std"], "mean_reversion.bb_std"
            ),
        }
        
        # Constrain breakout parameters
        curr_b = current_params["breakout"]
        new_b = new_params["breakout"]
        constrained["breakout"] = {
            "enabled": new_b["enabled"],
            "lookback_bars": int(round(constrain_value(
                curr_b["lookback_bars"], new_b["lookback_bars"], "breakout.lookback_bars"
            ))),
            "min_breakout_pct": constrain_value(
                curr_b["min_breakout_pct"], new_b["min_breakout_pct"], "breakout.min_breakout_pct"
            ),
        }
        
        return constrained

    async def _log_optimization_run(self, context: dict, result) -> int:
        """Log optimization run to database."""
        async with self.db.acquire() as conn:
            run_id = await conn.fetchval("""
                INSERT INTO optimization_runs (
                    context_hours, context_days,
                    prompt_tokens, completion_tokens,
                    raw_response, parsed_params,
                    reasoning_summary, confidence_score,
                    status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
                RETURNING run_id
            """,
                len(context.get('recent_hours', [])),
                len(context.get('daily_summaries', [])),
                result.prompt_tokens,
                result.completion_tokens,
                result.raw_response,
                str(result.new_params) if result.new_params else None,
                result.reasoning,
                result.confidence
            )
            return run_id

    async def _update_run_status(
        self,
        run_id: int,
        status: str,
        error_message: str = None,
        applied_version: int = None
    ):
        """Update optimization run status."""
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE optimization_runs
                SET status = $2, completed_at = NOW(),
                    error_message = $3, applied_version = $4
                WHERE run_id = $1
            """, run_id, status, error_message, applied_version)

    async def _sleep_until_next_hour(self):
        """Sleep until the start of the next hour."""
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        sleep_seconds = (next_hour - now).total_seconds()

        self.log.info(
            f"[OPTIMIZER] Sleeping {sleep_seconds/60:.1f} minutes until next hour ({next_hour})"
        )

        # Sleep in chunks to allow graceful shutdown
        while sleep_seconds > 0 and self.running:
            chunk = min(60, sleep_seconds)
            await asyncio.sleep(chunk)
            sleep_seconds -= chunk

    async def run_manual_optimization(self) -> dict:
        """
        Run optimization manually (for testing or on-demand).

        Returns:
            Dict with result details
        """
        self.log.info("[OPTIMIZER] Running manual optimization...")

        # Collect current metrics first
        await self.collector.collect_hourly_metrics(
            self.config_manager.current_version
        )

        # Build context
        current_params = self.config_manager.current_config
        context = await self.summarizer.get_context_for_llm(current_params)
        context_str = self.summarizer.format_for_prompt(context)

        # Call DeepSeek
        result = await self.optimizer.optimize_parameters(context_str, current_params)

        response = {
            "action": result.action,
            "confidence": result.confidence,
            "reasoning": result.reasoning,
            "suggested_params": result.new_params,
            "tokens_used": result.prompt_tokens + result.completion_tokens
        }

        if result.success and result.confidence >= self.min_confidence:
            # Apply changes
            version_id = await self.config_manager.apply_new_config(
                result.new_params,
                source="llm",
                reasoning=result.reasoning
            )
            response["applied"] = True
            response["version_id"] = version_id
            self.last_optimization = datetime.now(timezone.utc)
        else:
            response["applied"] = False
            response["reason"] = (
                "Low confidence" if result.confidence < self.min_confidence
                else result.reasoning
            )

        return response

    async def get_status(self) -> dict:
        """Get current optimization status for dashboard."""
        health = await self.safety.get_current_health()

        return {
            "running": self.running,
            "last_optimization": self.last_optimization.isoformat() if self.last_optimization else None,
            "current_version": self.config_manager.current_version,
            "health": health,
            "min_confidence": self.min_confidence,
            "cooldown_hours": self.min_hours_between_optimizations
        }
