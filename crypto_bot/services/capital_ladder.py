"""
Capital Ladder Service — Progressive scale-up with objective criteria.

Evaluates live performance at each capital level and sends ntfy notifications
when conditions are met to scale up, hold, or regress. Never auto-changes
capital, leverage, or position size — only measures and notifies.

Persists state to ~/.hlquantbot/capital_ladder.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseService
from .message_bus import Message, MessageBus
from ..core.enums import Topic

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("HLQUANTBOT_DATA_DIR", str(Path.home() / ".hlquantbot")))
STATE_FILE = DATA_DIR / "capital_ladder.json"

# Minimum trades before REGRESS can trigger
REGRESS_MIN_TRADES = 15


@dataclass
class LevelConfig:
    """Config for a single ladder level (from trading.yaml)."""
    level: int
    label: str
    target_capital_usd: float
    min_closed_trades: int
    min_live_days: int
    min_profit_factor: float
    min_net_pnl_usd: float
    max_drawdown_pct: float
    min_maker_fill_ratio_pct: float


@dataclass
class LevelMetrics:
    """Computed metrics for the current level period."""
    closed_trades: int = 0
    live_days: int = 0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    maker_fills: int = 0
    taker_fallbacks: int = 0
    skipped_entries: int = 0
    maker_fill_ratio_pct: float = 100.0
    pnl_by_symbol: Dict[str, float] = field(default_factory=dict)
    biggest_symbol_share_pct: float = 0.0
    worst_day_loss_pct: float = 0.0
    zero_trade_days: int = 0
    execution_errors: int = 0


@dataclass
class LadderState:
    """Persisted ladder state."""
    current_level: int = 0
    level_label: str = "test_65"
    target_capital_usd: float = 65.0
    started_at: str = ""
    baseline_equity: float = 0.0
    closed_trades_at_start: int = 0
    status: str = "TRACKING"  # LOCKED | TRACKING | READY_TO_SCALE | HOLD | REGRESS
    last_evaluation_at: str = ""
    last_status_sent_hash: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    peak_equity: float = 0.0


class CapitalLadderService(BaseService):
    """Evaluates performance at each capital level, sends ntfy alerts."""

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        config: Optional[Dict[str, Any]] = None,
        whatsapp: Optional[Any] = None,
        performance_monitor: Optional[Any] = None,
        exchange: Optional[Any] = None,
    ) -> None:
        super().__init__(
            name="capital_ladder",
            bus=bus,
            config=config or {},
            loop_interval_seconds=300,  # Evaluate every 5 min
        )
        self._whatsapp = whatsapp
        self._perf_monitor = performance_monitor
        self._exchange = exchange
        self._enabled = False
        self._levels: List[LevelConfig] = []
        self._state = LadderState()

        # Parse config
        ladder_cfg = (config or {}).get("capital_ladder", {})
        self._enabled = ladder_cfg.get("enabled", False)
        if not self._enabled:
            return

        for lc in ladder_cfg.get("levels", []):
            self._levels.append(LevelConfig(
                level=lc["level"],
                label=lc["label"],
                target_capital_usd=lc["target_capital_usd"],
                min_closed_trades=lc["min_closed_trades"],
                min_live_days=lc["min_live_days"],
                min_profit_factor=lc["min_profit_factor"],
                min_net_pnl_usd=lc["min_net_pnl_usd"],
                max_drawdown_pct=lc["max_drawdown_pct"],
                min_maker_fill_ratio_pct=lc["min_maker_fill_ratio_pct"],
            ))

        initial_level = ladder_cfg.get("current_level", 0)
        self._config_level = initial_level

    async def _on_start(self) -> None:
        if not self._enabled:
            self._logger.info("Capital ladder disabled")
            return

        self._load_state()

        # Detect level change from config
        if self._state.current_level != self._config_level:
            old = self._state.current_level
            self._promote_to_level(self._config_level)
            await self._send_level_changed(old, self._config_level)

        # Fetch baseline equity from exchange if missing
        if self._state.baseline_equity <= 0 and self._exchange:
            try:
                account = await self._exchange.get_account_state()
                equity = float(account.get("equity", 0))
                if equity > 0:
                    self._state.baseline_equity = equity
                    self._state.peak_equity = equity
                    self._logger.info("Baseline equity set: $%.2f", equity)
                    self._save_state()
            except Exception as e:
                self._logger.warning("Could not fetch baseline equity: %s", e)

        # Initialize new state if first run
        if not self._state.started_at:
            level_cfg = self._get_level_config(self._state.current_level)
            if level_cfg:
                self._state.level_label = level_cfg.label
                self._state.target_capital_usd = level_cfg.target_capital_usd
            self._state.started_at = datetime.now(timezone.utc).isoformat()
            self._state.status = "TRACKING"
            self._state.closed_trades_at_start = self._get_total_closed_trades()
            self._save_state()

        if self.bus:
            await self.subscribe(Topic.FILLS, self._on_trade_closed)

        self._logger.info(
            "Capital ladder started: level=%d (%s), target=$%.0f, status=%s",
            self._state.current_level,
            self._state.level_label,
            self._state.target_capital_usd,
            self._state.status,
        )

    async def _on_stop(self) -> None:
        self._save_state()

    async def _run_iteration(self) -> None:
        """Periodic evaluation."""
        if not self._enabled:
            return
        await self._evaluate()

    async def _health_check_impl(self) -> bool:
        return True

    # =========================================================================
    # Event Handling
    # =========================================================================

    async def _on_trade_closed(self, message: Message) -> None:
        """Evaluate ladder after each closed trade."""
        if not self._enabled:
            return
        payload = message.payload
        if payload.get("event") != "position_closed":
            return

        # Track maker/taker stats from fill metadata
        fill_type = payload.get("fill_type", "maker")
        if fill_type == "taker":
            self._state.peak_equity = max(
                self._state.peak_equity,
                self._state.baseline_equity + self._compute_metrics().net_pnl,
            )

        await self._evaluate()

    # =========================================================================
    # Metrics Computation
    # =========================================================================

    def _compute_metrics(self) -> LevelMetrics:
        """Compute metrics for the current level period only."""
        if not self._perf_monitor:
            return LevelMetrics()

        trades = self._perf_monitor._trades
        if not trades:
            return LevelMetrics()

        # Filter trades since level start
        level_start = self._state.started_at
        if not level_start:
            return LevelMetrics()

        try:
            start_dt = datetime.fromisoformat(level_start)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return LevelMetrics()

        level_trades = []
        for t in trades:
            try:
                t_dt = datetime.fromisoformat(t.closed_at)
                if t_dt.tzinfo is None:
                    t_dt = t_dt.replace(tzinfo=timezone.utc)
                if t_dt >= start_dt:
                    level_trades.append(t)
            except (ValueError, TypeError):
                continue

        m = LevelMetrics()
        m.closed_trades = len(level_trades)

        # Live days
        now = datetime.now(timezone.utc)
        m.live_days = max(1, (now - start_dt).days)

        if not level_trades:
            return m

        # P&L
        m.gross_profit = sum(t.realized_pnl for t in level_trades if t.realized_pnl > 0)
        m.gross_loss = abs(sum(t.realized_pnl for t in level_trades if t.realized_pnl < 0))
        m.net_pnl = m.gross_profit - m.gross_loss
        m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else 99.0

        # Drawdown
        equity_curve = []
        running = self._state.baseline_equity
        for t in level_trades:
            running += t.realized_pnl
            equity_curve.append(running)

        if equity_curve and self._state.baseline_equity > 0:
            peak = self._state.baseline_equity
            max_dd = 0.0
            for eq in equity_curve:
                peak = max(peak, eq)
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
            m.max_drawdown_pct = max_dd

        # P&L by symbol
        for t in level_trades:
            m.pnl_by_symbol[t.symbol] = m.pnl_by_symbol.get(t.symbol, 0.0) + t.realized_pnl

        if m.pnl_by_symbol and m.net_pnl != 0:
            max_sym_pnl = max(abs(v) for v in m.pnl_by_symbol.values())
            m.biggest_symbol_share_pct = (max_sym_pnl / max(abs(m.net_pnl), 0.01)) * 100

        # Worst day loss
        daily_pnl: Dict[str, float] = {}
        for t in level_trades:
            try:
                day = datetime.fromisoformat(t.closed_at).strftime("%Y-%m-%d")
                daily_pnl[day] = daily_pnl.get(day, 0.0) + t.realized_pnl
            except (ValueError, TypeError):
                continue

        if daily_pnl and self._state.baseline_equity > 0:
            worst = min(daily_pnl.values())
            if worst < 0:
                m.worst_day_loss_pct = abs(worst) / self._state.baseline_equity * 100

        # Zero trade days
        if m.live_days > 0:
            trade_days = len(daily_pnl)
            m.zero_trade_days = max(0, m.live_days - trade_days)

        # Maker fill ratio from execution engine stats (approximate from exit_reasons)
        maker_count = sum(1 for t in level_trades if getattr(t, "fill_type", "maker") == "maker")
        total_fills = len(level_trades)
        if total_fills > 0:
            m.maker_fill_ratio_pct = maker_count / total_fills * 100
        else:
            m.maker_fill_ratio_pct = 100.0

        return m

    # =========================================================================
    # Evaluation
    # =========================================================================

    async def _evaluate(self) -> None:
        """Evaluate current level status and send notifications."""
        level_cfg = self._get_level_config(self._state.current_level)
        if not level_cfg:
            return

        metrics = self._compute_metrics()
        self._state.last_evaluation_at = datetime.now(timezone.utc).isoformat()

        # Determine status
        old_status = self._state.status
        blockers = self._get_blockers(level_cfg, metrics)
        regress_reasons = self._check_regress(level_cfg, metrics)

        if regress_reasons:
            self._state.status = "REGRESS"
        elif not blockers:
            self._state.status = "READY_TO_SCALE"
        elif metrics.closed_trades < 5 and metrics.live_days < 3:
            self._state.status = "TRACKING"
        else:
            self._state.status = "HOLD"

        # Send deduped notification
        status_hash = self._compute_status_hash(metrics)

        if self._state.status != old_status or status_hash != self._state.last_status_sent_hash:
            if self._state.status == "READY_TO_SCALE":
                await self._send_scale_up_ready(level_cfg, metrics)
            elif self._state.status == "REGRESS":
                await self._send_do_not_scale(level_cfg, metrics, regress_reasons)
            else:
                await self._send_ladder_status(level_cfg, metrics, blockers)

            self._state.last_status_sent_hash = status_hash
            self._save_state()

    def _get_blockers(self, cfg: LevelConfig, m: LevelMetrics) -> List[str]:
        """Return list of blocking reasons preventing scale-up."""
        blockers = []
        if m.closed_trades < cfg.min_closed_trades:
            blockers.append(f"closed_trades {m.closed_trades}/{cfg.min_closed_trades}")
        if m.live_days < cfg.min_live_days:
            blockers.append(f"live_days {m.live_days}/{cfg.min_live_days}")
        if m.net_pnl < cfg.min_net_pnl_usd:
            blockers.append(f"net_pnl ${m.net_pnl:.2f} < ${cfg.min_net_pnl_usd:.2f}")
        if m.closed_trades >= 5 and m.profit_factor < cfg.min_profit_factor:
            blockers.append(f"profit_factor {m.profit_factor:.2f} < {cfg.min_profit_factor:.2f}")
        if m.max_drawdown_pct > cfg.max_drawdown_pct:
            blockers.append(f"max_dd {m.max_drawdown_pct:.1f}% > {cfg.max_drawdown_pct:.1f}%")
        if m.closed_trades >= 5 and m.maker_fill_ratio_pct < cfg.min_maker_fill_ratio_pct:
            blockers.append(f"maker_fill {m.maker_fill_ratio_pct:.0f}% < {cfg.min_maker_fill_ratio_pct:.0f}%")
        return blockers

    def _check_regress(self, cfg: LevelConfig, m: LevelMetrics) -> List[str]:
        """Check if conditions warrant REGRESS status."""
        reasons = []

        # Condition 1: 15+ trades, negative P&L, low PF
        if (m.closed_trades >= REGRESS_MIN_TRADES
                and m.net_pnl < 0
                and m.profit_factor < 0.95):
            reasons.append("net pnl negative with PF < 0.95")

        # Condition 2: drawdown exceeds threshold * 1.3
        if m.max_drawdown_pct > cfg.max_drawdown_pct * 1.3:
            reasons.append(f"drawdown {m.max_drawdown_pct:.1f}% > {cfg.max_drawdown_pct * 1.3:.1f}% (130% of limit)")

        # Condition 3: maker fill ratio far below threshold
        if m.closed_trades >= 10 and m.maker_fill_ratio_pct < cfg.min_maker_fill_ratio_pct - 15:
            reasons.append(f"maker fill {m.maker_fill_ratio_pct:.0f}% < {cfg.min_maker_fill_ratio_pct - 15:.0f}%")

        return reasons

    def _compute_status_hash(self, m: LevelMetrics) -> str:
        """Hash key metrics for dedup — changes when metrics meaningfully change."""
        key = f"{self._state.status}:{m.closed_trades}:{m.live_days}:{m.net_pnl:.1f}:{m.profit_factor:.2f}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    # =========================================================================
    # Notifications
    # =========================================================================

    async def _send_ladder_status(
        self,
        cfg: LevelConfig,
        m: LevelMetrics,
        blockers: List[str],
    ) -> None:
        """Send LADDER_STATUS ntfy notification."""
        if not self._whatsapp:
            return

        # Top symbols by P&L
        sorted_syms = sorted(m.pnl_by_symbol.items(), key=lambda x: x[1], reverse=True)[:5]
        sym_lines = "\n".join(
            f"  {'+'if v>=0 else ''}{v:.2f} {s}" for s, v in sorted_syms
        ) if sorted_syms else "  (no trades yet)"

        blocker_lines = "\n".join(f"  - {b}" for b in blockers) if blockers else "  (none)"

        text = (
            f"CAPITAL LADDER STATUS\n"
            f"Model: v7 | Universe: filtered\n"
            f"Mode: validation\n\n"
            f"Level: {self._state.current_level} / {cfg.label}\n"
            f"Target capital: ${cfg.target_capital_usd:.0f}\n"
            f"Status: {self._state.status}\n\n"
            f"Trades: {m.closed_trades} / {cfg.min_closed_trades}\n"
            f"Days: {m.live_days} / {cfg.min_live_days}\n"
            f"Net P&L: ${m.net_pnl:+.2f} / target ${cfg.min_net_pnl_usd:+.2f}\n"
            f"PF: {m.profit_factor:.2f} / target {cfg.min_profit_factor:.2f}\n"
            f"Max DD: {m.max_drawdown_pct:.1f}% / limit {cfg.max_drawdown_pct:.1f}%\n"
            f"Maker fill: {m.maker_fill_ratio_pct:.0f}% / target {cfg.min_maker_fill_ratio_pct:.0f}%\n\n"
            f"Top symbols:\n{sym_lines}\n\n"
            f"Blockers:\n{blocker_lines}"
        )

        try:
            await self._whatsapp._send_message(
                text,
                title=f"Ladder: {cfg.label} [{self._state.status}]",
                tags="ladder",
            )
        except Exception as e:
            self._logger.error("Failed to send ladder status: %s", e)

    async def _send_scale_up_ready(self, cfg: LevelConfig, m: LevelMetrics) -> None:
        """Send SCALE_UP_READY ntfy notification."""
        if not self._whatsapp:
            return

        next_cfg = self._get_level_config(self._state.current_level + 1)
        next_label = next_cfg.label if next_cfg else "(max level reached)"
        next_cap = next_cfg.target_capital_usd if next_cfg else cfg.target_capital_usd

        text = (
            f"SCALE UP READY\n"
            f"Model: v7 | Universe: filtered\n\n"
            f"Level completed: {cfg.label}\n"
            f"Suggested next: {next_label}\n"
            f"Suggested capital: ${next_cap:.0f}\n\n"
            f"Trades: {m.closed_trades}\n"
            f"Days: {m.live_days}\n"
            f"Net P&L: ${m.net_pnl:+.2f}\n"
            f"PF: {m.profit_factor:.2f}\n"
            f"Max DD: {m.max_drawdown_pct:.1f}%\n"
            f"Maker fill: {m.maker_fill_ratio_pct:.0f}%\n\n"
            f"Action:\n"
            f"  - you may increase capital to ${next_cap:.0f}\n"
            f"  - do not increase leverage\n"
            f"  - keep validation config"
        )

        try:
            await self._whatsapp._send_message(
                text,
                title=f"SCALE UP READY: {cfg.label} -> {next_label}",
                priority=True,
                tags="rocket",
            )
        except Exception as e:
            self._logger.error("Failed to send scale-up notification: %s", e)

    async def _send_do_not_scale(
        self,
        cfg: LevelConfig,
        m: LevelMetrics,
        reasons: List[str],
    ) -> None:
        """Send DO_NOT_SCALE ntfy notification."""
        if not self._whatsapp:
            return

        reason_lines = "\n".join(f"  - {r}" for r in reasons)

        text = (
            f"DO NOT SCALE\n"
            f"Model: v7 | Universe: filtered\n\n"
            f"Level: {cfg.label}\n"
            f"Status: REGRESS\n\n"
            f"Trades: {m.closed_trades} | PF: {m.profit_factor:.2f}\n"
            f"Net P&L: ${m.net_pnl:+.2f}\n"
            f"Max DD: {m.max_drawdown_pct:.1f}%\n\n"
            f"Reasons:\n{reason_lines}\n\n"
            f"Action:\n"
            f"  - do not increase capital\n"
            f"  - keep current size\n"
            f"  - review live performance"
        )

        try:
            await self._whatsapp._send_message(
                text,
                title=f"DO NOT SCALE: {cfg.label}",
                priority=True,
                tags="warning,skull",
            )
        except Exception as e:
            self._logger.error("Failed to send do-not-scale notification: %s", e)

    async def _send_level_changed(self, old: int, new: int) -> None:
        """Send LEVEL_CHANGED ntfy notification."""
        if not self._whatsapp:
            return

        new_cfg = self._get_level_config(new)
        label = new_cfg.label if new_cfg else f"level_{new}"
        cap = new_cfg.target_capital_usd if new_cfg else 0

        text = (
            f"LEVEL CHANGED\n"
            f"Model: v7 | Universe: filtered\n\n"
            f"Old level: {old}\n"
            f"New level: {new} ({label})\n"
            f"Target capital: ${cap:.0f}\n\n"
            f"Metrics have been reset for the new level.\n"
            f"Tracking starts now."
        )

        try:
            await self._whatsapp._send_message(
                text,
                title=f"Level Changed: {label}",
                tags="level_slider",
            )
        except Exception as e:
            self._logger.error("Failed to send level-changed notification: %s", e)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _get_level_config(self, level: int) -> Optional[LevelConfig]:
        for lc in self._levels:
            if lc.level == level:
                return lc
        return None

    def _get_total_closed_trades(self) -> int:
        if self._perf_monitor:
            return len(self._perf_monitor._trades)
        return 0

    def _promote_to_level(self, new_level: int) -> None:
        """Reset state for a new level."""
        # Record history
        self._state.history.append({
            "level": self._state.current_level,
            "label": self._state.level_label,
            "started_at": self._state.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": self._state.status,
        })

        level_cfg = self._get_level_config(new_level)
        self._state.current_level = new_level
        self._state.level_label = level_cfg.label if level_cfg else f"level_{new_level}"
        self._state.target_capital_usd = level_cfg.target_capital_usd if level_cfg else 0
        self._state.started_at = datetime.now(timezone.utc).isoformat()
        self._state.closed_trades_at_start = self._get_total_closed_trades()
        self._state.status = "TRACKING"
        self._state.last_status_sent_hash = ""
        self._state.peak_equity = self._state.baseline_equity
        self._save_state()

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_state(self) -> None:
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                self._state = LadderState(
                    current_level=data.get("current_level", 0),
                    level_label=data.get("level_label", "test_65"),
                    target_capital_usd=data.get("target_capital_usd", 65.0),
                    started_at=data.get("started_at", ""),
                    baseline_equity=data.get("baseline_equity", 0.0),
                    closed_trades_at_start=data.get("closed_trades_at_start", 0),
                    status=data.get("status", "TRACKING"),
                    last_evaluation_at=data.get("last_evaluation_at", ""),
                    last_status_sent_hash=data.get("last_status_sent_hash", ""),
                    history=data.get("history", []),
                    peak_equity=data.get("peak_equity", 0.0),
                )
                self._logger.info("Loaded ladder state: level=%d, status=%s",
                                  self._state.current_level, self._state.status)
        except Exception as e:
            self._logger.warning("Failed to load ladder state: %s", e)
            self._state = LadderState()

    def _save_state(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump(asdict(self._state), f, indent=2)
        except Exception as e:
            self._logger.error("Failed to save ladder state: %s", e)

    # =========================================================================
    # Public API
    # =========================================================================

    @property
    def ladder_status(self) -> Dict[str, Any]:
        """Current ladder status for health dashboard / validation status."""
        level_cfg = self._get_level_config(self._state.current_level)
        metrics = self._compute_metrics()
        blockers = self._get_blockers(level_cfg, metrics) if level_cfg else []
        next_cfg = self._get_level_config(self._state.current_level + 1)

        return {
            "ladder_current_level": self._state.current_level,
            "ladder_target_capital": self._state.target_capital_usd,
            "ladder_status": self._state.status,
            "ladder_ready_for_next_level": self._state.status == "READY_TO_SCALE",
            "ladder_blockers": blockers,
            "suggested_next_level": next_cfg.label if next_cfg else None,
            "metrics": asdict(metrics) if metrics else {},
        }
