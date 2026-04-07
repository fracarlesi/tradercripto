"""
FLAG-Trader Live Trading Agent
===============================

Integrates the trained FLAG-Trader model into the live trading pipeline.
Scans assets, builds prompts from candle data, and returns trade decisions.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .model import FlagTraderModel
from .prompt import PromptBuilder
from .trade_logger import FlagTradeLogger, TradeRecord
from .trade_memory_rag import TradeMemoryRAG

logger = logging.getLogger(__name__)



@dataclass
class TradeDecision:
    """A single trade decision from the FLAG-Trader model.

    STAGE A: model emits ``tp_pct`` / ``sl_pct`` at entry; bot then waits
    for TP / SL fill or expiry. ``expiry_at`` and ``k_candles`` carry the
    fixed-horizon expiry computed at decision time so the execution engine
    can place an unconditional time-based exit alongside TP/SL.
    """

    symbol: str
    action: int  # 0=Sell, 1=Hold, 2=Buy
    action_name: str
    confidence: float  # state value from value head
    log_prob: float
    tp_pct: float = 2.5  # model-predicted TP%
    sl_pct: float = 1.0  # model-predicted SL%
    correlation_id: Optional[str] = None  # end-to-end trace id
    # STAGE A forecast-mode fields
    expiry_at: Optional[datetime] = None
    k_candles: int = 34
    predicted_tp_pct: Optional[float] = None
    predicted_sl_pct: Optional[float] = None
    trade_id: Optional[str] = None


@dataclass
class FlagTraderConfig:
    """Configuration for the FLAG-Trader agent."""

    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    checkpoint_path: str = "models/flag_trader_qwen/final_model.pt"
    device: str = "cpu"
    scan_interval_seconds: int = 300
    max_assets_to_scan: int = 10
    candle_window: int = 20
    candle_interval: str = "15m"
    confidence_threshold: float = 0.6
    # STAGE A forecast-mode parameters (read from config.forecast.*).
    k_candles: int = 34
    candle_period_minutes: int = 15

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagTraderConfig:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class FlagTraderAgent:
    """Live trading agent using FLAG-Trader model.

    Scans assets, fetches candles, builds prompts, and gets model decisions.
    """

    def __init__(
        self,
        config: FlagTraderConfig,
        model: FlagTraderModel,
        prompt_builder: PromptBuilder,
        trade_logger: Optional[FlagTradeLogger] = None,
        trade_memory_rag: Optional[TradeMemoryRAG] = None,
    ) -> None:
        self.config = config
        self.model = model
        self.prompt_builder = prompt_builder
        self.trade_logger = trade_logger
        self.trade_memory_rag = trade_memory_rag
        self._trade_history: list[str] = []  # recent action names for prompt context

    async def scan_and_decide(
        self,
        assets: list[str],
        candle_fetcher: Any,
        portfolio: Optional[dict[str, float]] = None,
    ) -> list[TradeDecision]:
        """Scan assets and return trade decisions.

        Args:
            assets: List of asset symbols to scan.
            candle_fetcher: Object with async get_candles(symbol, interval, limit) method.
            portfolio: Current portfolio state for prompt context.

        Returns:
            List of TradeDecision for actionable signals (not Hold, above threshold).
        """
        if portfolio is None:
            portfolio = {"cash_balance": 0.0, "asset_position": 0.0, "total_account_value": 0.0}

        # Limit scan to top N assets
        scan_assets = assets[: self.config.max_assets_to_scan]
        decisions: list[TradeDecision] = []

        for symbol in scan_assets:
            try:
                decision = await self._evaluate_asset(symbol, candle_fetcher, portfolio)
                if decision is not None:
                    decisions.append(decision)
            except Exception as e:
                logger.warning("Error evaluating %s: %s", symbol, e)

        # Sort by absolute confidence descending
        decisions.sort(key=lambda d: abs(d.confidence), reverse=True)

        logger.info(
            "FLAG-Trader scan: %d assets, %d actionable decisions",
            len(scan_assets),
            len(decisions),
        )
        return decisions

    def _maybe_dump_prompt(self, symbol: str, prompt: str, correlation_id: str) -> None:
        """Optionally dump the full LLM prompt to disk for debugging.

        Activated by env var ``HLQUANTBOT_DUMP_PROMPTS=1``.  Files go under
        ``$HLQUANTBOT_DATA_DIR/prompts/`` (default ``~/.hlquantbot/prompts``).
        """
        if os.environ.get("HLQUANTBOT_DUMP_PROMPTS", "").strip() not in ("1", "true", "yes"):
            return
        try:
            base = Path(os.environ.get("HLQUANTBOT_DATA_DIR", str(Path.home() / ".hlquantbot")))
            dump_dir = base / "prompts"
            dump_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            fname = dump_dir / f"{ts}_{symbol}_{correlation_id}.txt"
            fname.write_text(prompt)
        except Exception as e:  # pragma: no cover - debug helper
            logger.debug("prompt dump failed for %s: %s", symbol, e)

    async def _evaluate_asset(
        self,
        symbol: str,
        candle_fetcher: Any,
        portfolio: dict[str, float],
    ) -> Optional[TradeDecision]:
        """Evaluate a single asset with the model.

        Returns TradeDecision if action is Buy/Sell and above threshold, else None.
        """
        candles_raw = await candle_fetcher.get_candles(
            symbol,
            interval=self.config.candle_interval,
            limit=self.config.candle_window + 5,  # extra buffer
        )

        if not candles_raw or len(candles_raw) < self.config.candle_window:
            logger.debug("Insufficient candle data for %s: %d candles", symbol, len(candles_raw) if candles_raw else 0)
            return None

        candles = self._candles_to_prompt_format(candles_raw)
        history = {
            "recent_rewards": [],
            "net_values": [],
            "actions": self._trade_history[-10:],
        }

        # RAG: find similar past trades for prompt injection
        similar_text = ""
        similar: list[dict] = []
        if self.trade_memory_rag:
            ms_dict = self._build_market_state_dict(candles)
            similar = self.trade_memory_rag.find_similar_trades(symbol, ms_dict)
            similar_text = self.trade_memory_rag.format_for_prompt(similar, symbol=symbol)

        prompt = self.prompt_builder.build_prompt(candles, portfolio, history, similar_trades_text=similar_text)

        correlation_id = uuid.uuid4().hex[:12]
        self._maybe_dump_prompt(symbol, prompt, correlation_id)

        logger.info(
            "FLAG-Trader INPUT | cid=%s | %s | candles=%d | last_close=%.2f | portfolio=$%.2f | similar_trades=%d",
            correlation_id, symbol, len(candles), candles[-1]["close"], portfolio.get("total_account_value", 0),
            len(similar),
        )
        logger.debug("FLAG-Trader PROMPT | %s | %s", symbol, prompt[:500])

        start = time.monotonic()
        action_id, state_value, log_prob, tp_pct, sl_pct = self.model.get_action(prompt)  # pyright: ignore[reportAssignmentType]  # torch/SDK typing
        elapsed = time.monotonic() - start
        action_name = {0: "SELL", 1: "HOLD", 2: "BUY"}.get(action_id, "HOLD")

        logger.info(
            "FLAG-Trader | %s | action=%s | value=%.4f | tp=%.1f%% | sl=%.1f%% | time=%.1fs",
            symbol, action_name, state_value, tp_pct, sl_pct, elapsed,
        )

        # Log every decision for retraining data (non-blocking)
        if self.trade_logger:
            try:
                closes = [c["close"] for c in candles]
                candles_summary = {
                    "last_close": closes[-1] if closes else 0.0,
                    "pct_change_20": ((closes[-1] / closes[0]) - 1) * 100 if len(closes) >= 2 and closes[0] != 0 else 0.0,
                    "volume_avg": sum(c["volume"] for c in candles) / len(candles) if candles else 0.0,
                }
                ms_summary = self._build_market_state_dict(candles) if candles else None
                # STAGE A: pre-compute predicted prices and expiry for the
                # forecast-mode pipeline. The execution engine will place
                # TP/SL atomically at entry and a time-based expiry exit.
                last_close = float(closes[-1]) if closes else 0.0
                is_long = action_id == 2
                pred_tp_price: Optional[float] = None
                pred_sl_price: Optional[float] = None
                if last_close > 0 and tp_pct > 0:
                    pred_tp_price = last_close * (
                        1 + tp_pct / 100 if is_long else 1 - tp_pct / 100
                    )
                if last_close > 0 and sl_pct > 0:
                    pred_sl_price = last_close * (
                        1 - sl_pct / 100 if is_long else 1 + sl_pct / 100
                    )
                now_utc = datetime.now(timezone.utc)
                expiry_at = now_utc + timedelta(
                    minutes=self.config.k_candles * self.config.candle_period_minutes
                )
                record = TradeRecord(
                    timestamp=now_utc.isoformat(),
                    symbol=symbol,
                    action=action_name,
                    action_id=action_id,
                    confidence=state_value,
                    log_prob=float(log_prob),
                    candles_summary=candles_summary,
                    portfolio=portfolio,
                    market_state_summary=ms_summary,
                    prompt_summary=prompt[:200],
                    predicted_tp_pct=float(tp_pct),
                    predicted_sl_pct=float(sl_pct),
                    predicted_tp_price=pred_tp_price,
                    predicted_sl_price=pred_sl_price,
                    expiry_at=expiry_at.isoformat(),
                    k_candles=self.config.k_candles,
                    candle_interval_sec=self.config.candle_period_minutes * 60,
                )
                self.trade_logger.log_decision(record)
                # Stash trade_id (assigned by log_decision) so callers can
                # propagate it through the TradeIntent → ExecutionPosition
                # chain and use it to write the per-trade sidecar at exit.
                self._last_trade_id = record.trade_id
                self._last_expiry_at = expiry_at
            except Exception as e:
                logger.warning("Trade logger failed (non-blocking): %s", e)

        # Only return actionable decisions (Buy/Sell) above confidence threshold
        if action_id == 1:  # Hold
            return None

        if abs(state_value) < self.config.confidence_threshold:
            logger.debug(
                "SKIP %s: confidence %.4f < threshold %.4f",
                symbol, abs(state_value), self.config.confidence_threshold,
            )
            return None

        return TradeDecision(
            symbol=symbol,
            action=action_id,
            action_name=action_name,
            confidence=state_value,
            log_prob=float(log_prob),
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            correlation_id=correlation_id,
            expiry_at=getattr(self, "_last_expiry_at", None),
            k_candles=self.config.k_candles,
            predicted_tp_pct=float(tp_pct),
            predicted_sl_pct=float(sl_pct),
            trade_id=getattr(self, "_last_trade_id", None),
        )

    # Removed in STAGE A (forecast-mode simplification): the bot no longer
    # re-evaluates open positions with the LLM. Exits are TP / SL / expiry.

    @staticmethod
    def _candles_to_prompt_format(candles_raw: list[dict]) -> list[dict[str, float]]:
        """Convert raw candle dicts from HyperliquidClient to PromptBuilder format.

        Input format (from API): {t, o, h, l, c, v}
        Output format (for PromptBuilder): {open, high, low, close, volume}
        """
        result: list[dict[str, float]] = []
        for c in candles_raw:
            result.append({
                "open": float(c.get("o", c.get("open", 0))),
                "high": float(c.get("h", c.get("high", 0))),
                "low": float(c.get("l", c.get("low", 0))),
                "close": float(c.get("c", c.get("close", 0))),
                "volume": float(c.get("v", c.get("volume", 0))),
            })
        return result

    @staticmethod
    def _build_market_state_dict(candles: list[dict[str, float]]) -> dict:
        """Build a lightweight market state dict from candles for RAG matching.

        Computes RSI(14), ATR%(14), and EMA9 slope from candle data.
        Returns dict with keys: rsi, adx, regime, atr_pct, ema9_slope.
        """
        if len(candles) < 15:
            return {}

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        # RSI(14) — simple Wilder smoothing
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]
        period = 14
        if len(gains) >= period:
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
            for i in range(period, len(gains)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
            rsi = 100.0 - (100.0 / (1.0 + rs))
        else:
            rsi = 50.0

        # ATR%(14)
        trs = []
        for i in range(1, len(candles)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        atr = sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0
        atr_pct = (atr / closes[-1] * 100) if closes[-1] != 0 else 0.0

        # EMA9 slope (sign only — positive or negative)
        ema9_slope = 0.0
        if len(closes) >= 13:
            # Simple: compare last close vs 4 bars ago as proxy
            ema9_slope = closes[-1] - closes[-5] if len(closes) >= 5 else 0.0

        return {
            "rsi": round(rsi, 1),
            "adx": None,  # not computable from simple candles
            "regime": None,  # not computable from simple candles
            "atr_pct": round(atr_pct, 2),
            "ema9_slope": round(ema9_slope, 4),
        }

    def record_action(self, action_name: str) -> None:
        """Record a trade action for history context in future prompts."""
        self._trade_history.append(action_name)
        if len(self._trade_history) > 50:
            self._trade_history = self._trade_history[-50:]
