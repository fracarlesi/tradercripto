"""Liquidation Sniping Strategy - Trade liquidation cascades."""

import logging
from decimal import Decimal
from typing import List, Optional, Dict
from datetime import datetime, timezone

from ...core.models import (
    ProposedTrade,
    AccountState,
    Bar,
    MarketContext,
    Position,
)
from ...core.enums import StrategyId, Side, OrderType, MarketRegime
from ...config.settings import Settings
from .hft_base import HFTBaseStrategy


logger = logging.getLogger(__name__)


# Regime-based direction filtering for counter-trend strategy
# In TREND_UP: only LONG allowed (buy dips) - don't short pumps against the trend
# In TREND_DOWN: only SHORT allowed (sell pumps) - don't buy dips against the trend
# HIGH_VOLATILITY is where liquidations happen - both directions OK
LIQSNIPE_REGIME_DIRECTIONS = {
    MarketRegime.TREND_UP: [Side.LONG],       # Only buy dips in uptrends
    MarketRegime.TREND_DOWN: [Side.SHORT],    # Only short pumps in downtrends
    MarketRegime.RANGE_BOUND: [Side.LONG, Side.SHORT],
    MarketRegime.HIGH_VOLATILITY: [Side.LONG, Side.SHORT],  # Core regime for liq sniping
    MarketRegime.LOW_VOLATILITY: [],   # No liquidations in calm markets
    MarketRegime.UNCERTAIN: [],        # Disable when uncertain
}


class LiquidationSnipingStrategy(HFTBaseStrategy):
    """
    Liquidation Sniping 2.0 - CORE HFT Strategy for High Volatility Days.

    SPEC REQUIREMENTS:
    - Triple confirmation required: OI + Price + Volume spikes
    - TP: 0.40%-0.60% (dynamic, scaled with cascade strength)
    - SL: max 0.20% (fixed for optimal R:R)
    - MIN_RR: 1.5
    - Fee-aware: Net profit >= 0.20% after roundtrip fees (0.04%)

    DETECTION CRITERIA (ALL must be satisfied):
    1. OI Spike: ΔOI >= ±10% over 30-60s window
       - OI DROP >= 10% → Long liquidations → Trade LONG (buy the dip)
       - OI SPIKE >= 10% → Short liquidations → Trade SHORT (sell the pump)

    2. Price Spike: >= ±0.35% in < 60s

    3. Volume Spike: >= 1.5x average of last 20 bars

    LOGIC:
    - Detect liquidation cascades via triple confirmation
    - Wait for cascade exhaustion (price reversal signal)
    - Enter counter-trend with dynamic TP/SL
    - Very short hold time (45s max) - capture the snap-back

    This is a counter-trend strategy that requires:
    - Fast detection with high precision (triple confirmation)
    - Quick entry after cascade peak
    - Aggressive R:R optimization (2.0-3.0 typical)
    - Fee-aware profit calculation

    Key indicators:
    - OI spike >= ±10% (positions being liquidated)
    - Price spike >= ±0.35% (cascade price impact)
    - Volume surge >= 1.5x (liquidation execution volume)
    - Exhaustion signal (price reversal)

    Performance expectations:
    - Win rate: 55-65% (high precision due to triple confirmation)
    - Avg R:R: 2.0-3.0
    - Signal frequency: 3-8 per day (across all symbols)
    - Net profit per trade: 0.36%-0.56% (after fees)
    """

    def __init__(self, settings: Settings):
        super().__init__(settings, StrategyId.LIQUIDATION_SNIPING)

        # =======================================================================
        # LIQUIDATION SNIPING 2.0 - CORE STRATEGY PARAMETERS
        # =======================================================================
        # REQUISITI SPEC: Questa strategia deve essere la CORE strategy per P&L
        # su giornate ad alta volatilità, con detection precisa di liquidation cascades.

        # 1. OI Detection: ΔOI >= ±10% su finestra 30-60s
        self.oi_spike_threshold_pct = self._get_param('oi_spike_threshold_pct', Decimal("0.10"))
        self.oi_spike_window_seconds = self._get_param_int('oi_spike_window_seconds', 60)

        # 2. Price Spike: >= ±0.35% in < 60s
        self.price_spike_threshold_pct = self._get_param('price_spike_threshold_pct', Decimal("0.0035"))

        # 3. Volume Spike: >= 1.5x media ultimi 20 sample
        self.volume_spike_threshold_pct = self._get_param('volume_spike_threshold_pct', Decimal("1.5"))
        self.volume_lookback_bars = 20  # Per calcolare media volume

        # Entry timing: delay minimo per permettere exhaustion
        self.entry_delay_ms = self._get_param_int('entry_delay_ms', 100)

        # TP/SL CONSTRAINTS (dalla specifica)
        # - TP: 0.40%-0.60% (gross)
        # - SL: max 0.20%
        # - MIN_RR: 1.5
        # - Fee-awareness: TP_net >= 0.20% dopo fee roundtrip
        self.min_tp_pct = Decimal("0.004")  # 0.40%
        self.max_tp_pct = Decimal("0.006")  # 0.60%
        self.max_sl_pct = Decimal("0.002")  # 0.20% MAX
        self.min_rr_ratio = Decimal("1.5")

        # Fee roundtrip (maker entry + maker exit)
        self.fee_roundtrip_pct = Decimal("0.0004")  # 0.04% (2x 0.02%)
        self.min_net_profit_pct = Decimal("0.002")  # 0.20% net dopo fees

        # Tracking structures
        self._oi_history: Dict[str, List[tuple]] = {}  # symbol -> [(timestamp, oi)]
        self._price_history: Dict[str, List[tuple]] = {}  # symbol -> [(timestamp, price)]
        self._volume_history: Dict[str, List[tuple]] = {}  # symbol -> [(timestamp, volume)]
        self._last_liquidation_signal: Dict[str, datetime] = {}
        self._cascade_detected: Dict[str, dict] = {}  # symbol -> cascade info

        # Volume bars tracking per calcolare media mobile
        self._volume_bars: Dict[str, List[Decimal]] = {}  # symbol -> [volumes]

        # Current market regime (set by bot)
        self._current_regime: MarketRegime = MarketRegime.UNCERTAIN

    def set_regime(self, regime: MarketRegime):
        """Set current market regime. Called by bot."""
        self._current_regime = regime

    def is_direction_allowed(self, side: Side) -> bool:
        """
        Check if trade direction is allowed in current regime.

        Counter-trend logic:
        - TREND_UP: Only buy dips (LONG), don't short pumps
        - TREND_DOWN: Only short pumps (SHORT), don't buy dips
        - HIGH_VOLATILITY: Both OK (core liq sniping regime)
        """
        allowed_directions = LIQSNIPE_REGIME_DIRECTIONS.get(
            self._current_regime, []
        )
        return side in allowed_directions

    def _get_param(self, name: str, default: Decimal) -> Decimal:
        if self._hft_config:
            return Decimal(str(getattr(self._hft_config, name, default)))
        return default

    def _get_param_int(self, name: str, default: int) -> int:
        if self._hft_config:
            return int(getattr(self._hft_config, name, default))
        return default

    async def evaluate(
        self,
        symbol: str,
        bars: List[Bar],
        context: MarketContext,
        account: AccountState,
        position: Optional[Position] = None,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate liquidation sniping strategy.

        SPEC REQUISITI:
        1. Detect ΔOI >= ±10% su 30-60s
        2. Detect Price Spike >= ±0.35% in <60s
        3. Confirm Volume Spike >= 1.5x media 20 bars
        4. Enter SOLO se tutti i criteri sono soddisfatti

        Steps:
        1. Update tracking structures (OI, price, volume)
        2. Detect liquidation cascade (triple confirmation)
        3. Wait for cascade exhaustion
        4. Enter counter-trend con TP/SL ottimizzati
        """
        if not self.can_signal_hft(symbol):
            return None

        # Se abbiamo posizione, verifica timeout
        if position:
            if self.should_close_for_timeout(symbol):
                return self._create_close_proposal(symbol, position, context)
            return None

        current_price = context.current_price
        if not current_price or current_price == 0:
            return None

        # Update tracking structures
        self._update_price_history(symbol, current_price)

        # Update volume bars tracking (per calcolare media)
        if bars:
            self._update_volume_bars(symbol, bars)

        # Get OI from context
        open_interest = getattr(context, 'open_interest', None)
        if open_interest:
            self._update_oi_history(symbol, open_interest)

        # Get volume from context (fallback se bars non disponibile)
        volume = getattr(context, 'volume_24h', None)
        if volume:
            self._update_volume_history(symbol, volume)

        # CORE: Detect liquidation cascade con triple confirmation
        cascade = self._detect_liquidation_cascade(
            symbol=symbol,
            current_price=current_price,
            context=context,
            bars=bars,
        )

        if cascade:
            # Wait for exhaustion and enter counter-trend
            return self._evaluate_cascade_entry(symbol, cascade, current_price, context)

        return None

    def _update_price_history(self, symbol: str, price: Decimal):
        """Update price history for a symbol."""
        now = datetime.now(timezone.utc)

        if symbol not in self._price_history:
            self._price_history[symbol] = []

        self._price_history[symbol].append((now, price))

        # Clean old entries
        cutoff = now.timestamp() - self.oi_spike_window_seconds
        self._price_history[symbol] = [
            (ts, p) for ts, p in self._price_history[symbol]
            if ts.timestamp() >= cutoff
        ]

    def _update_oi_history(self, symbol: str, oi: Decimal):
        """Update OI history for a symbol."""
        now = datetime.now(timezone.utc)

        if symbol not in self._oi_history:
            self._oi_history[symbol] = []

        self._oi_history[symbol].append((now, oi))

        # Clean old entries
        cutoff = now.timestamp() - self.oi_spike_window_seconds
        self._oi_history[symbol] = [
            (ts, o) for ts, o in self._oi_history[symbol]
            if ts.timestamp() >= cutoff
        ]

    def _update_volume_history(self, symbol: str, volume: Decimal):
        """
        Update volume history for a symbol.

        Mantiene una finestra sliding per calcolare volume spike ratio.
        """
        now = datetime.now(timezone.utc)

        if symbol not in self._volume_history:
            self._volume_history[symbol] = []

        self._volume_history[symbol].append((now, volume))

        # Clean old entries (finestra 60s)
        cutoff = now.timestamp() - self.oi_spike_window_seconds
        self._volume_history[symbol] = [
            (ts, v) for ts, v in self._volume_history[symbol]
            if ts.timestamp() >= cutoff
        ]

    def _update_volume_bars(self, symbol: str, bars: List[Bar]):
        """
        Update volume bars tracking per media mobile.

        SPEC REQUISITO: VolumeSpike >= 1.5x media ultimi 20 bars
        """
        if not bars:
            return

        if symbol not in self._volume_bars:
            self._volume_bars[symbol] = []

        # Estrai volumi
        volumes = [bar.volume for bar in bars[-self.volume_lookback_bars:]]
        self._volume_bars[symbol] = volumes

    def _check_volume_surge(self, symbol: str, bars: Optional[List[Bar]] = None) -> tuple[bool, Decimal]:
        """
        Check if volume has surged.

        SPEC REQUISITO: VolumeSpike >= 1.5x media ultimi 20 bars

        Returns:
            (is_spike, spike_ratio)
        """
        # Priorità: usa bars se forniti (più preciso)
        if bars and len(bars) >= 2:
            if len(bars) < self.volume_lookback_bars:
                # Non abbastanza dati, usa tutti i bars disponibili
                lookback = max(2, len(bars) - 1)
                avg_volume = sum(b.volume for b in bars[:-1]) / lookback
            else:
                # Calcola media su ultimi 20 bars (escluso current)
                lookback = min(self.volume_lookback_bars, len(bars) - 1)
                avg_volume = sum(b.volume for b in bars[-lookback-1:-1]) / lookback

            current_volume = bars[-1].volume

            if avg_volume == 0:
                # Se avg=0 ma current>0, è sicuramente uno spike
                return (current_volume > 0, Decimal("999") if current_volume > 0 else Decimal("0"))

            volume_ratio = current_volume / avg_volume
            is_spike = volume_ratio >= self.volume_spike_threshold_pct

            logger.debug(
                f"Volume spike check for {symbol}: "
                f"current={current_volume:.2f}, avg={avg_volume:.2f}, "
                f"ratio={volume_ratio:.2f}x, threshold={self.volume_spike_threshold_pct}x, "
                f"spike={is_spike}"
            )

            return (is_spike, volume_ratio)

        # Fallback: usa volume_history (meno preciso)
        volume_history = self._volume_history.get(symbol, [])
        if len(volume_history) < 3:
            # Not enough data, assume no spike per essere conservativi
            return (False, Decimal("0"))

        # Compare recent volume to average
        avg_volume = sum(v for _, v in volume_history[:-1]) / (len(volume_history) - 1)
        current_volume = volume_history[-1][1]

        if avg_volume == 0:
            return (current_volume > 0, Decimal("999") if current_volume > 0 else Decimal("0"))

        volume_ratio = current_volume / avg_volume
        return (volume_ratio >= self.volume_spike_threshold_pct, volume_ratio)

    def _detect_liquidation_cascade(
        self,
        symbol: str,
        current_price: Decimal,
        context: MarketContext,
        bars: Optional[List[Bar]] = None,
    ) -> Optional[dict]:
        """
        Detect liquidation cascade con TRIPLE CONFIRMATION.

        SPEC REQUISITI - Un trade è valido SOLO se TUTTI i criteri sono soddisfatti:
        1. ΔOI >= +10% (long liquidations) o -10% (short liquidations)
        2. VolumeSpike >= 1.5x media ultimi 20 bars
        3. PriceSpike >= ±0.35% in finestra < 60s

        Returns:
            dict con info cascade se detected, None altrimenti
        """
        now = datetime.now(timezone.utc)

        # Check if we already have an active cascade detection
        existing = self._cascade_detected.get(symbol)
        if existing:
            # Check if still valid (within entry window)
            elapsed = (now - existing["detected_at"]).total_seconds() * 1000
            if elapsed < self.entry_delay_ms * 5:  # 5x delay window to capture entry
                return existing

        # =======================================================================
        # TRIPLE CONFIRMATION LOGIC
        # =======================================================================
        confirmations = {
            "oi_spike": False,
            "price_spike": False,
            "volume_spike": False,
        }
        cascade_direction = None
        cascade_strength = Decimal(0)
        oi_change_pct = Decimal(0)
        price_change_pct = Decimal(0)
        volume_ratio = Decimal(0)

        # -----------------------------------------------------------------------
        # 1. OI SPIKE DETECTION: ΔOI >= ±10%
        # -----------------------------------------------------------------------
        oi_history = self._oi_history.get(symbol, [])
        if len(oi_history) >= 2:
            oldest_oi = oi_history[0][1]
            current_oi = oi_history[-1][1]

            if oldest_oi > 0:
                oi_change_pct = (current_oi - oldest_oi) / oldest_oi

                # OI DROP >= 10% -> LONG liquidations (price crashed)
                if oi_change_pct <= -self.oi_spike_threshold_pct:
                    confirmations["oi_spike"] = True
                    cascade_direction = Side.LONG  # Counter-trend: buy the dip
                    cascade_strength = abs(oi_change_pct)

                    logger.debug(
                        f"[{symbol}] OI DROP detected: {oi_change_pct:.2%} "
                        f"(threshold: -{self.oi_spike_threshold_pct:.2%})"
                    )

                # OI INCREASE >= 10% -> SHORT liquidations (price pumped)
                elif oi_change_pct >= self.oi_spike_threshold_pct:
                    confirmations["oi_spike"] = True
                    cascade_direction = Side.SHORT  # Counter-trend: short the pump
                    cascade_strength = abs(oi_change_pct)

                    logger.debug(
                        f"[{symbol}] OI SPIKE detected: {oi_change_pct:.2%} "
                        f"(threshold: +{self.oi_spike_threshold_pct:.2%})"
                    )

        # -----------------------------------------------------------------------
        # 2. PRICE SPIKE DETECTION: >= ±0.35% in <60s
        # -----------------------------------------------------------------------
        price_history = self._price_history.get(symbol, [])
        if len(price_history) >= 2:
            oldest_price = price_history[0][1]
            price_change_pct = (current_price - oldest_price) / oldest_price

            if abs(price_change_pct) >= self.price_spike_threshold_pct:
                confirmations["price_spike"] = True

                # Se non abbiamo direction da OI, usa price direction
                if not cascade_direction:
                    # Price DROP -> trade long (buy the dip)
                    # Price SPIKE -> trade short (sell the pump)
                    cascade_direction = Side.LONG if price_change_pct < 0 else Side.SHORT
                    cascade_strength = abs(price_change_pct)

                logger.debug(
                    f"[{symbol}] PRICE SPIKE detected: {price_change_pct:+.2%} "
                    f"(threshold: ±{self.price_spike_threshold_pct:.2%})"
                )

        # -----------------------------------------------------------------------
        # 3. VOLUME SPIKE DETECTION: >= 1.5x media 20 bars
        # -----------------------------------------------------------------------
        volume_spike, volume_ratio = self._check_volume_surge(symbol, bars)
        if volume_spike:
            confirmations["volume_spike"] = True
            logger.debug(
                f"[{symbol}] VOLUME SPIKE detected: {volume_ratio:.2f}x "
                f"(threshold: {self.volume_spike_threshold_pct}x)"
            )

        # =======================================================================
        # VALIDATION: Tutti e 3 i criteri devono essere soddisfatti
        # =======================================================================
        all_confirmed = all(confirmations.values())

        if not all_confirmed:
            # Log quale criterio è mancante (per debug)
            missing = [k for k, v in confirmations.items() if not v]
            logger.debug(
                f"[{symbol}] Liquidation cascade INCOMPLETE - missing: {missing} | "
                f"OI={confirmations['oi_spike']}, "
                f"Price={confirmations['price_spike']}, "
                f"Volume={confirmations['volume_spike']}"
            )
            return None

        # =======================================================================
        # CASCADE CONFIRMED - Tutti i criteri soddisfatti
        # =======================================================================
        if not cascade_direction:
            logger.warning(f"[{symbol}] Cascade detected but no direction determined")
            return None

        cascade_info = {
            "symbol": symbol,
            "direction": cascade_direction,
            "strength": cascade_strength,
            "detected_at": now,
            "entry_price": current_price,
            "oi_change_pct": oi_change_pct,
            "price_change_pct": price_change_pct,
            "volume_ratio": volume_ratio,
        }
        self._cascade_detected[symbol] = cascade_info

        logger.info(
            f"✓ LIQUIDATION CASCADE CONFIRMED: {symbol} {cascade_direction.value} | "
            f"ΔOI={oi_change_pct:+.2%}, "
            f"ΔPrice={price_change_pct:+.2%}, "
            f"VolSpike={volume_ratio:.2f}x, "
            f"Strength={cascade_strength:.2%}"
        )

        return cascade_info

    def _evaluate_cascade_entry(
        self,
        symbol: str,
        cascade: dict,
        current_price: Decimal,
        context: MarketContext,
    ) -> Optional[ProposedTrade]:
        """
        Evaluate entry dopo cascade detection.

        SPEC REQUISITI:
        - TP: 0.40%-0.60% (gross)
        - SL: max 0.20%
        - MIN_RR: 1.5
        - Fee-awareness: TP_net >= 0.20% dopo roundtrip fees

        Wait for exhaustion signal before entering.
        """
        now = datetime.now(timezone.utc)
        detected_at = cascade["detected_at"]
        elapsed_ms = (now - detected_at).total_seconds() * 1000

        # Wait for minimum delay (let cascade exhaust)
        if elapsed_ms < self.entry_delay_ms:
            return None

        # Check for exhaustion signals
        if not self._is_cascade_exhausted(symbol, cascade, current_price):
            # Cascade still active, wait
            if elapsed_ms < self.entry_delay_ms * 3:
                return None
            # Timeout - cascade too long, skip
            if symbol in self._cascade_detected:
                del self._cascade_detected[symbol]
            return None

        # ===================================================================
        # EXHAUSTION CONFIRMED - ENTER COUNTER-TREND
        # ===================================================================
        side = cascade["direction"]
        strength = cascade["strength"]

        # REGIME FILTER: Check if direction is allowed in current regime
        if not self.is_direction_allowed(side):
            logger.info(
                f"[{symbol}] Liquidation snipe {side.value} blocked by regime filter "
                f"(regime={self._current_regime.value}) - skipping counter-trend trade"
            )
            # Clear cascade detection
            if symbol in self._cascade_detected:
                del self._cascade_detected[symbol]
            return None

        # Calculate TP/SL dinamicamente basato su cascade strength
        # Più forte è la cascade, più aggressivo il TP
        tp_pct, sl_pct = self._calculate_dynamic_tp_sl(strength)

        # Validate constraints
        if not self._validate_tp_sl_constraints(tp_pct, sl_pct):
            logger.warning(
                f"[{symbol}] TP/SL validation failed: TP={tp_pct:.2%}, SL={sl_pct:.2%}"
            )
            # Clear cascade e skip trade
            if symbol in self._cascade_detected:
                del self._cascade_detected[symbol]
            return None

        # Calculate exact prices
        if side == Side.LONG:
            tp_price = current_price * (1 + tp_pct)
            sl_price = current_price * (1 - sl_pct)
        else:
            tp_price = current_price * (1 - tp_pct)
            sl_price = current_price * (1 + sl_pct)

        # Calculate expected R:R
        risk_reward = tp_pct / sl_pct if sl_pct > 0 else Decimal("0")

        # Calculate net profit after fees
        net_profit_pct = tp_pct - self.fee_roundtrip_pct

        # Calculate confidence based on cascade strength and confirmations
        oi_change = abs(cascade.get("oi_change_pct", Decimal("0")))
        price_change = abs(cascade.get("price_change_pct", Decimal("0")))
        volume_ratio = cascade.get("volume_ratio", Decimal("0"))

        # Confidence scaling: più forti i segnali, più alta la confidence
        confidence = min(
            Decimal("0.85"),
            Decimal("0.55") + (oi_change * Decimal("2")) + (price_change * Decimal("3"))
        )

        logger.info(
            f"→ LIQUIDATION SNIPE ENTRY: {symbol} {side.value} @ {current_price} | "
            f"TP={tp_pct:.2%} ({tp_price}), SL={sl_pct:.2%} ({sl_price}), "
            f"R:R={risk_reward:.2f}, NetProfit={net_profit_pct:.2%}, "
            f"Cascade: ΔOI={oi_change:.2%}, ΔPrice={price_change:.2%}, Vol={volume_ratio:.1f}x"
        )

        # Clear cascade detection
        if symbol in self._cascade_detected:
            del self._cascade_detected[symbol]

        self._last_liquidation_signal[symbol] = now

        # Create custom proposal (NON usare create_hft_proposal per TP/SL custom)
        return self._create_custom_proposal(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            tp_price=tp_price,
            sl_price=sl_price,
            context=context,
            confidence=confidence,
            reason=(
                f"Liquidation cascade {strength:.2%} | "
                f"ΔOI={oi_change:.2%}, ΔP={price_change:.2%}, Vol={volume_ratio:.1f}x"
            ),
        )

    def _calculate_dynamic_tp_sl(self, cascade_strength: Decimal) -> tuple[Decimal, Decimal]:
        """
        Calculate TP/SL dinamicamente basato su cascade strength.

        SPEC CONSTRAINTS:
        - TP: 0.40%-0.60%
        - SL: max 0.20%
        - MIN_RR: 1.5

        Logic:
        - Cascade più forte -> TP più alto (maggiore probabilità di bounce ampio)
        - SL sempre fisso al max (0.20%) per ottimizzare R:R
        """
        # SL fisso al massimo per ottimizzare R:R
        sl_pct = self.max_sl_pct  # 0.20%

        # TP scalato linearmente con cascade strength
        # strength 0.10 (10%) -> TP 0.40%
        # strength 0.20 (20%) -> TP 0.50%
        # strength 0.30+ (30%+) -> TP 0.60%
        if cascade_strength <= Decimal("0.10"):
            tp_pct = self.min_tp_pct  # 0.40%
        elif cascade_strength >= Decimal("0.30"):
            tp_pct = self.max_tp_pct  # 0.60%
        else:
            # Linear interpolation
            ratio = (cascade_strength - Decimal("0.10")) / Decimal("0.20")
            tp_pct = self.min_tp_pct + (self.max_tp_pct - self.min_tp_pct) * ratio

        return tp_pct, sl_pct

    def _validate_tp_sl_constraints(self, tp_pct: Decimal, sl_pct: Decimal) -> bool:
        """
        Validate TP/SL rispettano tutti i constraints.

        SPEC REQUIREMENTS:
        1. TP >= 0.40% (MIN_TP)
        2. SL <= 0.20% (MAX_SL)
        3. R:R >= 1.5
        4. Net profit after fees >= 0.20%
        """
        # 1. TP minimum
        if tp_pct < self.min_tp_pct:
            logger.debug(f"TP {tp_pct:.2%} < min {self.min_tp_pct:.2%}")
            return False

        # 2. SL maximum
        if sl_pct > self.max_sl_pct:
            logger.debug(f"SL {sl_pct:.2%} > max {self.max_sl_pct:.2%}")
            return False

        # 3. R:R minimum
        if sl_pct > 0:
            rr_ratio = tp_pct / sl_pct
            if rr_ratio < self.min_rr_ratio:
                logger.debug(f"R:R {rr_ratio:.2f} < min {self.min_rr_ratio}")
                return False

        # 4. Net profit after fees
        net_profit = tp_pct - self.fee_roundtrip_pct
        if net_profit < self.min_net_profit_pct:
            logger.debug(
                f"Net profit {net_profit:.2%} < min {self.min_net_profit_pct:.2%} "
                f"(TP={tp_pct:.2%}, Fees={self.fee_roundtrip_pct:.2%})"
            )
            return False

        return True

    def _create_custom_proposal(
        self,
        symbol: str,
        side: Side,
        entry_price: Decimal,
        tp_price: Decimal,
        sl_price: Decimal,
        context: MarketContext,
        confidence: Decimal,
        reason: str,
    ) -> ProposedTrade:
        """
        Create custom proposal con TP/SL specifici.

        Similar a create_hft_proposal ma con TP/SL custom.
        """
        from ...core.enums import OrderType

        # Record signal
        self.record_signal_hft(symbol)

        # Get allocation from config
        allocation_pct = Decimal("0.01")  # Default 1%
        if self._hft_config:
            allocation_pct = Decimal(str(getattr(self._hft_config, 'max_position_pct', 0.01)))

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=side,
            notional_usd=Decimal("1000"),  # Overridden by position sizer
            risk_per_trade=Decimal("70"),  # 0.7% of $10k
            entry_type=OrderType.LIMIT_GTX,  # Post-only maker
            entry_price=entry_price,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            confidence=confidence,
            reason=f"[LIQSNIPE-2.0] {reason}",
            market_context=context,
        )

    def _is_cascade_exhausted(
        self,
        symbol: str,
        cascade: dict,
        current_price: Decimal,
    ) -> bool:
        """
        Check if the cascade has exhausted and reversal starting.

        Exhaustion signals:
        - Price starting to reverse from cascade direction
        - Volume decreasing
        - Rate of price change slowing
        """
        price_history = self._price_history.get(symbol, [])
        if len(price_history) < 3:
            return True  # Not enough data, allow entry

        # Check last few price points for reversal
        recent_prices = [p for _, p in price_history[-3:]]

        cascade_direction = cascade["direction"]

        if cascade_direction == Side.LONG:
            # We want to go long after a drop
            # Exhaustion = price stopped dropping, starting to rise
            return recent_prices[-1] > recent_prices[-2]
        else:
            # We want to short after a spike
            # Exhaustion = price stopped rising, starting to drop
            return recent_prices[-1] < recent_prices[-2]

    def _create_close_proposal(
        self,
        symbol: str,
        position: Position,
        context: MarketContext,
    ) -> ProposedTrade:
        """Create proposal to close position."""
        close_side = Side.SHORT if position.side == Side.LONG else Side.LONG

        return ProposedTrade(
            strategy_id=self.strategy_id,
            symbol=symbol,
            side=close_side,
            notional_usd=position.notional_value,
            risk_per_trade=Decimal(0),
            confidence=Decimal("1.0"),
            reason="[Liquidation Snipe] Position timeout",
            market_context=context,
        )
