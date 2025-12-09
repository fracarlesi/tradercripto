"""Market regime detection using GPT."""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, Optional, List

from openai import AsyncOpenAI

from ..core.models import RegimeAnalysis, AccountState, MarketContext, StrategyMetrics, Bar
from ..core.enums import MarketRegime, StrategyId
from ..config.settings import Settings
from .prompts import REGIME_DETECTION_SYSTEM_PROMPT, REGIME_DETECTION_USER_TEMPLATE


def calculate_atr(bars: List, period: int = 14) -> Decimal:
    """Calculate Average True Range from bars."""
    if not bars or len(bars) < period + 1:
        return Decimal("0")

    tr_values = []
    for i in range(-period, 0):
        high = bars[i].high if hasattr(bars[i], 'high') else Decimal(str(bars[i].get('high', 0)))
        low = bars[i].low if hasattr(bars[i], 'low') else Decimal(str(bars[i].get('low', 0)))
        prev_close = bars[i - 1].close if hasattr(bars[i - 1], 'close') else Decimal(str(bars[i - 1].get('close', 0)))

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        tr_values.append(tr)

    return sum(tr_values) / len(tr_values) if tr_values else Decimal("0")


def calculate_volatility_percentile(bars: List, lookback: int = 100) -> float:
    """Calculate current volatility percentile (0-100)."""
    if not bars or len(bars) < lookback:
        return 50.0  # Default to median

    # Calculate rolling standard deviations
    returns = []
    for i in range(-lookback, -1):
        if i - 1 >= -len(bars):
            close = bars[i].close if hasattr(bars[i], 'close') else Decimal(str(bars[i].get('close', 0)))
            prev_close = bars[i - 1].close if hasattr(bars[i - 1], 'close') else Decimal(str(bars[i - 1].get('close', 0)))
            if prev_close > 0:
                ret = float((close - prev_close) / prev_close)
                returns.append(ret)

    if len(returns) < 20:
        return 50.0

    # Calculate rolling std
    import statistics
    window = 20
    stds = []
    for i in range(len(returns) - window + 1):
        stds.append(statistics.stdev(returns[i:i + window]))

    if not stds:
        return 50.0

    current_std = stds[-1]
    sorted_stds = sorted(stds)
    percentile = (sorted_stds.index(current_std) / len(sorted_stds)) * 100
    return percentile


def get_price_history_summary(bars: List, periods: List[int] = [5, 15, 60]) -> Dict:
    """Get price change summary over different periods."""
    if not bars:
        return {}

    current_price = bars[-1].close if hasattr(bars[-1], 'close') else Decimal(str(bars[-1].get('close', 0)))
    summary = {}

    for period in periods:
        if len(bars) > period:
            past_price = bars[-period - 1].close if hasattr(bars[-period - 1], 'close') else Decimal(str(bars[-period - 1].get('close', 0)))
            if past_price > 0:
                change_pct = float((current_price - past_price) / past_price * 100)
                summary[f"{period}m_change_pct"] = round(change_pct, 2)

    return summary


logger = logging.getLogger(__name__)


class RegimeDetector:
    """
    Detects market regime using GPT analysis.

    Runs periodically (not on every tick) to classify market conditions
    and provide risk adjustment recommendations.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = settings.openai

        self._client: Optional[AsyncOpenAI] = None
        self._last_analysis: Optional[RegimeAnalysis] = None
        self._last_run: Optional[datetime] = None

        # Cache validity
        self._cache_duration = timedelta(minutes=self.config.regime_detection_interval_minutes)

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled and bool(self.config.api_key)

    async def _get_client(self) -> AsyncOpenAI:
        """Get or create OpenAI/DeepSeek client."""
        if self._client is None:
            # Support both OpenAI and DeepSeek (OpenAI-compatible API)
            base_url = getattr(self.config, 'base_url', None)
            if base_url:
                self._client = AsyncOpenAI(
                    api_key=self.config.api_key,
                    base_url=base_url
                )
            else:
                self._client = AsyncOpenAI(api_key=self.config.api_key)
        return self._client

    async def detect_regime(
        self,
        contexts: Dict[str, MarketContext],
        account: AccountState,
        strategy_metrics: Optional[Dict[StrategyId, StrategyMetrics]] = None,
        bars_data: Optional[Dict[str, List]] = None,
        force: bool = False,
    ) -> RegimeAnalysis:
        """
        Detect current market regime.

        Args:
            contexts: Market context for each symbol
            account: Current account state
            strategy_metrics: Recent strategy performance metrics
            bars_data: Historical bars per symbol for ATR/volatility calculation
            force: Force new analysis even if cache is valid

        Returns:
            RegimeAnalysis with regime classification and recommendations
        """
        # Check cache
        if not force and self._is_cache_valid():
            return self._last_analysis

        if not self.is_enabled:
            return self._default_analysis()

        try:
            analysis = await self._run_analysis(contexts, account, strategy_metrics, bars_data)
            self._last_analysis = analysis
            self._last_run = datetime.now(timezone.utc)
            return analysis

        except Exception as e:
            logger.error(f"Regime detection failed: {e}")
            # Return last analysis or default
            if self._last_analysis:
                return self._last_analysis
            return self._default_analysis()

    def _is_cache_valid(self) -> bool:
        """Check if cached analysis is still valid."""
        if not self._last_analysis or not self._last_run:
            return False
        return datetime.now(timezone.utc) - self._last_run < self._cache_duration

    def _default_analysis(self) -> RegimeAnalysis:
        """Return default analysis when AI is unavailable."""
        return RegimeAnalysis(
            timestamp=datetime.now(timezone.utc),
            regime=MarketRegime.UNCERTAIN,
            confidence=Decimal("0.5"),
            risk_adjustment=Decimal("1.0"),
            analysis="AI regime detection unavailable - using default",
        )

    async def _run_analysis(
        self,
        contexts: Dict[str, MarketContext],
        account: AccountState,
        strategy_metrics: Optional[Dict[StrategyId, StrategyMetrics]],
        bars_data: Optional[Dict[str, List]] = None,
    ) -> RegimeAnalysis:
        """Run GPT analysis."""
        client = await self._get_client()

        # Build asset data string
        asset_data_lines = []
        for symbol, ctx in contexts.items():
            asset_data_lines.append(
                f"### {symbol}\n"
                f"- Price: ${ctx.mid_price:.2f}\n"
                f"- Funding Rate: {ctx.funding_rate:.4%}\n"
                f"- Open Interest: ${ctx.open_interest:,.0f}\n"
                f"- 24h Volume: ${ctx.volume_24h:,.0f}\n"
            )
        asset_data = "\n".join(asset_data_lines)

        # Build technical data string (Context Pack 2.0: ATR, volatility, price history)
        technical_data_lines = []
        if bars_data:
            for symbol, bars in bars_data.items():
                if bars and len(bars) > 15:
                    atr = calculate_atr(bars)
                    vol_pct = calculate_volatility_percentile(bars)
                    price_history = get_price_history_summary(bars)

                    current_price = bars[-1].close if hasattr(bars[-1], 'close') else Decimal(str(bars[-1].get('close', 0)))
                    atr_pct = float(atr / current_price * 100) if current_price > 0 else 0

                    tech_line = f"### {symbol}\n"
                    tech_line += f"- ATR (14): {atr:.2f} ({atr_pct:.2f}%)\n"
                    tech_line += f"- Volatility Percentile: {vol_pct:.1f}%\n"

                    if price_history:
                        for key, val in price_history.items():
                            tech_line += f"- {key}: {val:+.2f}%\n"

                    technical_data_lines.append(tech_line)

        technical_data = "\n".join(technical_data_lines) if technical_data_lines else "No technical data available (insufficient bar history)"

        # Build strategy performance string
        if strategy_metrics:
            perf_lines = []
            for sid, metrics in strategy_metrics.items():
                perf_lines.append(
                    f"- {sid.value}: {metrics.total_trades} trades, "
                    f"WR: {metrics.win_rate:.1%}, PF: {metrics.profit_factor:.2f}"
                )
            strategy_performance = "\n".join(perf_lines)
        else:
            strategy_performance = "No recent trades"

        # Calculate drawdown
        if account.daily_starting_equity and account.daily_starting_equity > 0:
            total_drawdown = (
                (account.daily_starting_equity - account.equity) /
                account.daily_starting_equity
            )
        else:
            total_drawdown = Decimal(0)

        # Build user prompt
        user_prompt = REGIME_DETECTION_USER_TEMPLATE.format(
            timestamp=datetime.now(timezone.utc).isoformat(),
            environment="TESTNET" if self.settings.is_testnet else "PRODUCTION",
            asset_data=asset_data,
            technical_data=technical_data,
            daily_pnl_pct=float(account.daily_pnl_pct),
            total_drawdown=float(total_drawdown),
            position_count=account.position_count,
            current_leverage=float(account.current_leverage),
            strategy_performance=strategy_performance,
        )

        # Call LLM (OpenAI or DeepSeek)
        # Note: DeepSeek V3.2-Speciale doesn't support response_format
        is_deepseek = "deepseek" in getattr(self.config, 'base_url', '').lower()

        logger.info(f"Calling DeepSeek API: base_url={self.config.base_url}, model={self.config.model}")

        call_params = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": REGIME_DETECTION_SYSTEM_PROMPT + "\n\nIMPORTANT: Respond with valid JSON only, no markdown formatting."},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "timeout": 90.0,  # SDK-level timeout for deepseek-reasoner (30-60s typical)
        }

        # Only add response_format for OpenAI (not DeepSeek)
        if not is_deepseek:
            call_params["response_format"] = {"type": "json_object"}

        # Add timeout to prevent indefinite blocking with retry logic
        max_retries = 2
        last_error = None

        for attempt in range(max_retries):
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(**call_params),
                    timeout=90.0  # 90 second asyncio timeout for deepseek-reasoner
                )
                break  # Success - exit retry loop
            except asyncio.TimeoutError:
                last_error = Exception("API timeout")
                logger.warning(f"Regime detection API call timed out after 90s (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # Short delay before retry
                    continue
                else:
                    raise last_error
            except Exception as e:
                last_error = e
                logger.warning(f"Regime detection API call failed: {e} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # Short delay before retry
                    continue
                else:
                    raise

        # Parse response
        content = response.choices[0].message.content

        # Extract JSON from response (handle markdown code blocks)
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        result = json.loads(content)

        # Map regime string to enum
        regime_str = result.get("regime", "uncertain").lower()
        regime_map = {
            "trend_up": MarketRegime.TREND_UP,
            "trend_down": MarketRegime.TREND_DOWN,
            "range_bound": MarketRegime.RANGE_BOUND,
            "high_volatility": MarketRegime.HIGH_VOLATILITY,
            "low_volatility": MarketRegime.LOW_VOLATILITY,
            "uncertain": MarketRegime.UNCERTAIN,
        }
        regime = regime_map.get(regime_str, MarketRegime.UNCERTAIN)

        # Parse asset regimes
        asset_regimes = {}
        for symbol, regime_val in result.get("asset_regimes", {}).items():
            asset_regimes[symbol] = regime_map.get(regime_val.lower(), MarketRegime.UNCERTAIN)

        # Parse strategy allocation suggestions
        suggested_allocations = None
        if "recommendations" in result and "allocations" in result["recommendations"]:
            suggested_allocations = {
                StrategyId(k): Decimal(str(v))
                for k, v in result["recommendations"]["allocations"].items()
            }

        analysis = RegimeAnalysis(
            timestamp=datetime.now(timezone.utc),
            regime=regime,
            confidence=Decimal(str(result.get("confidence", 0.5))),
            asset_regimes=asset_regimes,
            suggested_allocations=suggested_allocations,
            risk_adjustment=Decimal(str(result.get("risk_adjustment", 1.0))),
            analysis=result.get("analysis", ""),
            valid_until=datetime.now(timezone.utc) + self._cache_duration,
        )

        logger.info(
            f"Regime detected: {regime.value} "
            f"(confidence: {analysis.confidence:.2f}, "
            f"risk adj: {analysis.risk_adjustment:.2f})"
        )
        # Log the AI analysis for debugging
        if analysis.analysis:
            logger.info(f"AI analysis: {analysis.analysis[:200]}")

        return analysis

    def get_current_regime(self) -> MarketRegime:
        """Get current regime (from cache)."""
        if self._last_analysis:
            return self._last_analysis.regime
        return MarketRegime.UNCERTAIN

    def get_risk_adjustment(self) -> Decimal:
        """Get current risk adjustment multiplier."""
        if self._last_analysis:
            return self._last_analysis.risk_adjustment
        return Decimal("1.0")
