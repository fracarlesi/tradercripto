"""
Pivot Point Calculator Service

Calcola pivot points classici per identificare supporti e resistenze.
I pivot points sono livelli di prezzo dove il prezzo tende a rimbalzare.

Reference: Video Rizzo 1:16-2:27, 15:00-15:50
Formula: https://www.investopedia.com/terms/p/pivotpoint.asp
"""

from typing import Dict, List, Optional
from datetime import datetime, timedelta
import asyncio
import logging

logger = logging.getLogger(__name__)


class PivotCalculator:
    """
    Calcola pivot points (PP, R1-R3, S1-S3) per trading analysis.

    Pivot Points sono livelli di prezzo calcolati da High/Low/Close del periodo precedente.
    Sono pattern ricorrenti che identificano supporti (S) e resistenze (R).

    Usage:
        calculator = PivotCalculator(hyperliquid_service)
        pivots = await calculator.calculate_pivot_points("BTC", current_price=104298.0)

        # Output:
        # {
        #     "PP": 104000.0,
        #     "R1": 105500.0,
        #     "R2": 106800.0,
        #     "R3": 108100.0,
        #     "S1": 102500.0,
        #     "S2": 101200.0,
        #     "S3": 99900.0,
        #     "current_price": 104298.0,
        #     "distances": {...},
        #     "interpretation": "Prezzo vicino a S1, possibile rimbalzo rialzista",
        # }
    """

    def __init__(self, hyperliquid_service=None):
        """
        Args:
            hyperliquid_service: HyperliquidTradingService instance per fetch candles
        """
        self.hyperliquid = hyperliquid_service
        self.cache = {}  # Cache pivot points (TTL 1 giorno)
        self.cache_ttl = 86400  # 24 ore (pivot cambiano ogni giorno)

    async def calculate_pivot_points(
        self,
        symbol: str,
        current_price: Optional[float] = None,
        timeframe: str = "1d",
    ) -> Dict:
        """
        Calcola pivot points per un simbolo.

        Args:
            symbol: Simbolo crypto (es. "BTC")
            current_price: Prezzo corrente (se None, usa ultimo close)
            timeframe: Timeframe per calcolo (default "1d" = daily pivot)

        Returns:
            Dict con PP, R1-R3, S1-S3, distances, interpretation
        """
        cache_key = f"{symbol}_{timeframe}"

        # Check cache
        if cache_key in self.cache:
            cached_time, cached_result = self.cache[cache_key]
            elapsed = (datetime.utcnow() - cached_time).seconds
            if elapsed < self.cache_ttl:
                logger.debug(f"Using cached pivot points for {symbol}")
                # Update current_price if provided
                if current_price:
                    cached_result = self._update_distances(cached_result, current_price)
                return cached_result

        try:
            # 1. Fetch previous period candle (es. ieri per daily pivot)
            logger.info(f"Calculating pivot points for {symbol} (timeframe={timeframe})")

            prev_candle = await self._get_previous_candle(symbol, timeframe)

            if not prev_candle:
                raise ValueError(f"No candle data available for {symbol}")

            high = float(prev_candle["high"])
            low = float(prev_candle["low"])
            close = float(prev_candle["close"])

            # 2. Calculate pivot points (formula classica)
            pivot_point = (high + low + close) / 3

            # Resistenze (sopra)
            r1 = 2 * pivot_point - low
            r2 = pivot_point + (high - low)
            r3 = high + 2 * (pivot_point - low)

            # Supporti (sotto)
            s1 = 2 * pivot_point - high
            s2 = pivot_point - (high - low)
            s3 = low - 2 * (high - pivot_point)

            # 3. Usa current_price o ultimo close
            price = current_price or close

            result = {
                "symbol": symbol,
                "timeframe": timeframe,
                "pivot_point": round(pivot_point, 2),
                "PP": round(pivot_point, 2),  # Alias
                "R1": round(r1, 2),
                "R2": round(r2, 2),
                "R3": round(r3, 2),
                "S1": round(s1, 2),
                "S2": round(s2, 2),
                "S3": round(s3, 2),
                "current_price": round(price, 2),
                "prev_candle": {
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close, 2),
                },
            }

            # 4. Calculate distances and interpretation
            result = self._calculate_distances(result)
            result = self._interpret_position(result)

            # Update cache
            self.cache[cache_key] = (datetime.utcnow(), result)

            logger.info(
                f"Pivot points for {symbol}: PP={pivot_point:.2f}, "
                f"R1={r1:.2f}, S1={s1:.2f}, Current={price:.2f}"
            )

            return result

        except Exception as e:
            logger.error(f"Failed to calculate pivot points for {symbol}", exc_info=True)
            return {
                "symbol": symbol,
                "error": str(e),
                "pivot_point": None,
            }

    def _calculate_distances(self, result: Dict) -> Dict:
        """
        Calcola distanze del prezzo corrente da ogni livello pivot.

        Aggiunge al result:
        - distances: Distanze assolute in USD
        - distances_pct: Distanze percentuali
        """
        price = result["current_price"]
        pp = result["PP"]

        # Validate price is valid for percentage calculation
        if price <= 0:
            raise ValueError(f"Invalid price {price}, cannot calculate percentage distances")

        # Distanze assolute
        distances = {
            "to_pp": round(price - pp, 2),
            "to_r1": round(price - result["R1"], 2),
            "to_r2": round(price - result["R2"], 2),
            "to_r3": round(price - result["R3"], 2),
            "to_s1": round(price - result["S1"], 2),
            "to_s2": round(price - result["S2"], 2),
            "to_s3": round(price - result["S3"], 2),
        }

        # Distanze percentuali
        distances_pct = {
            "to_pp": round((distances["to_pp"] / price) * 100, 2),
            "to_r1": round((distances["to_r1"] / price) * 100, 2),
            "to_r2": round((distances["to_r2"] / price) * 100, 2),
            "to_s1": round((distances["to_s1"] / price) * 100, 2),
            "to_s2": round((distances["to_s2"] / price) * 100, 2),
        }

        result["distances"] = distances
        result["distances_pct"] = distances_pct

        return result

    def _interpret_position(self, result: Dict) -> Dict:
        """
        Interpreta la posizione del prezzo rispetto ai pivot points.

        Logica:
        - Vicino a supporto (S1/S2) → Possibile rimbalzo SU (long opportunity)
        - Vicino a resistenza (R1/R2) → Possibile rimbalzo GIÙ (short opportunity)
        - Sopra PP → Bullish zone
        - Sotto PP → Bearish zone
        """
        price = result["current_price"]
        pp = result["PP"]
        r1 = result["R1"]
        r2 = result["R2"]
        s1 = result["S1"]
        s2 = result["S2"]

        dist_s1_pct = abs(result["distances_pct"]["to_s1"])
        dist_r1_pct = abs(result["distances_pct"]["to_r1"])

        # Determina zona
        if price > pp:
            zone = "bullish"  # Sopra pivot = zona rialzista
        else:
            zone = "bearish"  # Sotto pivot = zona ribassista

        # Determina azione suggerita
        interpretation = ""
        signal = "neutral"

        # Scenario 1: Vicino a supporto S1 (<2% distanza)
        if dist_s1_pct < 2.0 and price > s1:
            interpretation = f"Prezzo vicino a supporto S1 (${s1:.2f}, {dist_s1_pct:.2f}% distanza). Possibile RIMBALZO RIALZISTA. Considerare LONG se conferma."
            signal = "long_opportunity"

        # Scenario 2: Vicino a supporto S2 (<2% distanza)
        elif dist_s1_pct < 2.0 and price > s2:
            interpretation = f"Prezzo vicino a supporto S2 (${s2:.2f}). Supporto FORTE, alto rischio se rompe. Considerare LONG se conferma."
            signal = "long_opportunity"

        # Scenario 3: Vicino a resistenza R1 (<2% distanza)
        elif dist_r1_pct < 2.0 and price < r1:
            interpretation = f"Prezzo vicino a resistenza R1 (${r1:.2f}, {dist_r1_pct:.2f}% distanza). Possibile RIMBALZO RIBASSISTA. Considerare SHORT se conferma."
            signal = "short_opportunity"

        # Scenario 4: Vicino a resistenza R2 (<2% distanza)
        elif dist_r1_pct < 2.0 and price < r2:
            interpretation = f"Prezzo vicino a resistenza R2 (${r2:.2f}). Resistenza FORTE, difficile breakout. Considerare SHORT se conferma."
            signal = "short_opportunity"

        # Scenario 5: Sopra PP (zona bullish)
        elif price > pp:
            interpretation = f"Prezzo SOPRA pivot point (${pp:.2f}). Zona RIALZISTA. Bias LONG se rompe R1 (${r1:.2f})."
            signal = "bullish_zone"

        # Scenario 6: Sotto PP (zona bearish)
        else:
            interpretation = f"Prezzo SOTTO pivot point (${pp:.2f}). Zona RIBASSISTA. Bias SHORT se rompe S1 (${s1:.2f})."
            signal = "bearish_zone"

        result["zone"] = zone
        result["signal"] = signal
        result["interpretation"] = interpretation

        return result

    def _update_distances(self, cached_result: Dict, current_price: float) -> Dict:
        """
        Aggiorna solo current_price e distances senza ricalcolare pivot.
        """
        cached_result["current_price"] = round(current_price, 2)
        cached_result = self._calculate_distances(cached_result)
        cached_result = self._interpret_position(cached_result)
        return cached_result

    async def _get_previous_candle(self, symbol: str, timeframe: str) -> Optional[Dict]:
        """
        Fetch previous period candle (es. ieri per daily pivot).

        Args:
            symbol: Simbolo (es. "BTC")
            timeframe: "1d", "4h", "1h"

        Returns:
            Dict con high, low, close del periodo precedente
        """
        if not self.hyperliquid:
            raise ValueError("HyperliquidTradingService not initialized")

        try:
            # Converti timeframe in minuti
            interval_map = {
                "1d": 1440,  # 24 ore
                "4h": 240,
                "1h": 60,
            }
            interval_minutes = interval_map.get(timeframe, 1440)

            # Fetch 2 candele (ultima + penultima)
            end_time = int(datetime.utcnow().timestamp() * 1000)
            start_time = int(
                (datetime.utcnow() - timedelta(minutes=interval_minutes * 2)).timestamp()
                * 1000
            )

            info = self.hyperliquid.info
            candles = info.candles_snapshot(
                name=symbol,  # Hyperliquid SDK uses 'name' not 'coin'
                interval=timeframe,
                startTime=start_time,
                endTime=end_time,
            )

            if not candles or len(candles) < 1:
                logger.warning(f"No candle data for {symbol} {timeframe}")
                return None

            # Prendi penultima candela (periodo precedente completo)
            prev_candle = candles[-2] if len(candles) >= 2 else candles[-1]

            return {
                "time": prev_candle["t"],
                "open": float(prev_candle["o"]),
                "high": float(prev_candle["h"]),
                "low": float(prev_candle["l"]),
                "close": float(prev_candle["c"]),
            }

        except Exception as e:
            logger.error(
                f"Failed to fetch previous candle for {symbol} {timeframe}",
                exc_info=True,
            )
            return None

    def get_pivot_summary_for_ai(self, pivots: Dict) -> str:
        """
        Genera summary formattato per AI system prompt.

        Args:
            pivots: Output di calculate_pivot_points()

        Returns:
            String formattato per system prompt
        """
        if "error" in pivots or not pivots.get("pivot_point"):
            return "⚠️ Pivot points unavailable"

        symbol = pivots["symbol"]
        price = pivots["current_price"]
        pp = pivots["PP"]
        r1 = pivots["R1"]
        r2 = pivots["R2"]
        s1 = pivots["S1"]
        s2 = pivots["S2"]

        dist_pp = pivots["distances"]["to_pp"]
        dist_r1 = pivots["distances"]["to_r1"]
        dist_s1 = pivots["distances"]["to_s1"]

        interpretation = pivots["interpretation"]
        signal = pivots["signal"]

        # Emoji per segnale
        signal_emoji = {
            "long_opportunity": "🟢",
            "short_opportunity": "🔴",
            "bullish_zone": "🔵",
            "bearish_zone": "🟠",
            "neutral": "⚪",
        }.get(signal, "⚪")

        summary = f"""
### Pivot Points - {symbol} (Daily)

**Current Price**: ${price:,.2f}

**Pivot Levels**:
- R2: ${r2:,.2f} (resistance, {pivots['distances_pct']['to_r2']:.2f}% away)
- R1: ${r1:,.2f} (resistance, {dist_r1:+.2f} USD)
- **PP**: ${pp:,.2f} (pivot point, {dist_pp:+.2f} USD)
- S1: ${s1:,.2f} (support, {dist_s1:+.2f} USD)
- S2: ${s2:,.2f} (support, {pivots['distances_pct']['to_s2']:.2f}% away)

**Signal**: {signal_emoji} {signal.upper().replace('_', ' ')}

**Interpretation**: {interpretation}

**Trading Action**:
- If price BOUNCES at S1 → Consider LONG (target PP/R1)
- If price REJECTS at R1 → Consider SHORT (target PP/S1)
- If price BREAKS R1 → Strong bullish (target R2)
- If price BREAKS S1 → Strong bearish (target S2)
"""

        return summary.strip()


async def calculate_pivot_points_batch(
    symbols: List[str],
    prices: Dict[str, float],
    cache_manager=None,
) -> Dict[str, dict]:
    """
    Calculate pivot points for multiple symbols in batch (optimized for 142 symbols).

    This is the NEW API for the orchestrator system that efficiently handles
    batch calculations with caching.

    Args:
        symbols: List of symbols to calculate pivots for
        prices: Dict mapping symbol to current price
        cache_manager: Optional CacheManager instance (uses global if None)

    Returns:
        Dictionary mapping symbol to pivot data:
        {
            "BTC": {
                "PP": 101200.0,
                "R1": 103700.0,
                "R2": 104900.0,
                "R3": 106100.0,
                "S1": 99900.0,
                "S2": 98700.0,
                "S3": 97500.0,
                "current_zone": "bullish",
                "signal": "bullish_zone",
                "distance_to_support_pct": 2.5,
                "distance_to_resistance_pct": -0.5
            },
            ...
        }

    Example:
        >>> prices = {"BTC": 102450.0, "ETH": 3850.5, ...}
        >>> pivots = await calculate_pivot_points_batch(["BTC", "ETH"], prices)
        >>> btc_pivot = pivots["BTC"]["PP"]
    """
    logger.info(f"[BATCH] Calculating pivot points for {len(symbols)} symbols")

    # Use global cache manager if not provided
    if cache_manager is None:
        from services.orchestrator.cache_manager import get_cache_manager
        cache_manager = get_cache_manager()

    # Get calculator
    calculator = get_pivot_calculator()

    # Check cache first
    cached = cache_manager.get_batch("pivot_points", symbols)
    logger.info(f"[BATCH] Cache hit: {len(cached)}/{len(symbols)} symbols")

    # Find missing symbols
    missing_symbols = [s for s in symbols if s not in cached]

    # Calculate missing pivots in parallel
    if missing_symbols:
        logger.info(f"[BATCH] Calculating {len(missing_symbols)} missing pivots")

        tasks = [
            calculator.calculate_pivot_points(symbol, current_price=prices.get(symbol))
            for symbol in missing_symbols
        ]

        # Run in parallel with asyncio.gather
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for symbol, result in zip(missing_symbols, results):
            if isinstance(result, Exception):
                logger.error(f"[BATCH] Failed to calculate pivot for {symbol}: {result}")
                continue

            # Skip if error in result
            if "error" in result or not result.get("pivot_point"):
                logger.warning(f"[BATCH] No pivot data for {symbol}")
                continue

            # Skip if missing distances (incomplete calculation)
            if "distances" not in result or "distances_pct" not in result:
                logger.warning(f"[BATCH] Missing distances for {symbol}, skipping")
                continue

            # Convert to structured format for JSON builder
            try:
                structured = {
                    "PP": result["PP"],
                    "R1": result["R1"],
                    "R2": result["R2"],
                    "R3": result["R3"],
                    "S1": result["S1"],
                    "S2": result["S2"],
                    "S3": result["S3"],
                    "current_zone": result["zone"],
                    "signal": result["signal"],
                    "distance_to_support_pct": result["distances_pct"]["to_s1"],
                    "distance_to_resistance_pct": result["distances_pct"]["to_r1"],
                }

                # Cache for 1 hour
                cache_manager.set("pivot_points", symbol, structured, ttl=3600)
                cached[symbol] = structured
            except KeyError as e:
                logger.warning(f"[BATCH] Incomplete pivot data for {symbol}: missing {e}")
                continue

    logger.info(f"[BATCH] Completed: {len(cached)}/{len(symbols)} symbols with pivot data")

    return cached


# Singleton instance
pivot_calculator: Optional[PivotCalculator] = None


def get_pivot_calculator() -> PivotCalculator:
    """
    Get singleton instance of PivotCalculator.

    Usage:
        from services.market_data.pivot_calculator import get_pivot_calculator

        calculator = get_pivot_calculator()
        pivots = await calculator.calculate_pivot_points("BTC", 104298.0)
    """
    global pivot_calculator
    if pivot_calculator is None:
        # Import here to avoid circular dependency
        from services.trading.hyperliquid_trading_service import (
            hyperliquid_trading_service,
        )

        pivot_calculator = PivotCalculator(hyperliquid_trading_service)
    return pivot_calculator
