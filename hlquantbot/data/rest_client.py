"""REST client for Hyperliquid API."""

import asyncio
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import aiohttp

from ..core.models import AccountState, Position, MarketContext, Bar
from ..core.enums import Side, TimeFrame
from ..core.exceptions import DataFeedError, RateLimitError
from ..config.settings import Settings


class RateLimiter:
    """Simple rate limiter for API requests."""

    def __init__(self, max_requests: int, per_seconds: int = 60):
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self.requests: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Wait until a request can be made."""
        async with self._lock:
            now = time.time()
            # Remove old requests
            self.requests = [t for t in self.requests if now - t < self.per_seconds]

            if len(self.requests) >= self.max_requests:
                # Wait until oldest request expires
                sleep_time = self.per_seconds - (now - self.requests[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                self.requests = self.requests[1:]

            self.requests.append(time.time())


class HyperliquidRestClient:
    """Async REST client for Hyperliquid Info API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = f"{settings.hl_base_url}/info"
        self.rate_limiter = RateLimiter(
            max_requests=settings.hyperliquid.max_requests_per_minute
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._meta_cache: Optional[Dict] = None
        self._meta_cache_time: float = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, payload: Dict[str, Any]) -> Any:
        """Make a POST request to the info endpoint."""
        await self.rate_limiter.acquire()
        session = await self._get_session()

        try:
            async with session.post(self.base_url, json=payload) as response:
                if response.status == 429:
                    raise RateLimitError("Rate limit exceeded", retry_after=60)
                if response.status != 200:
                    text = await response.text()
                    raise DataFeedError(f"HTTP {response.status}: {text}", source="rest")
                return await response.json()
        except aiohttp.ClientError as e:
            raise DataFeedError(f"Connection error: {e}", source="rest")

    # -------------------------------------------------------------------------
    # Meta / Universe
    # -------------------------------------------------------------------------
    async def get_meta(self, use_cache: bool = True) -> Dict:
        """Get perpetuals metadata (universe, tick sizes, etc.)."""
        cache_duration = 300  # 5 minutes

        if use_cache and self._meta_cache:
            if time.time() - self._meta_cache_time < cache_duration:
                return self._meta_cache

        data = await self._post({"type": "meta"})
        self._meta_cache = data
        self._meta_cache_time = time.time()
        return data

    async def get_meta_and_asset_ctxs(self) -> Dict:
        """Get meta + asset contexts (OI, funding, prices)."""
        return await self._post({"type": "metaAndAssetCtxs"})

    # -------------------------------------------------------------------------
    # Market Data
    # -------------------------------------------------------------------------
    async def get_all_mids(self) -> Dict[str, Decimal]:
        """Get mid prices for all assets."""
        data = await self._post({"type": "allMids"})
        return {symbol: Decimal(str(price)) for symbol, price in data.items()}

    async def get_l2_book(self, symbol: str, n_levels: int = 20) -> Dict:
        """Get L2 order book snapshot."""
        return await self._post({
            "type": "l2Book",
            "coin": symbol,
            "nSigFigs": 5,
        })

    async def get_candles(
        self,
        symbol: str,
        interval: str = "15m",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Bar]:
        """Get historical candles."""
        # Note: Hyperliquid testnet requires startTime to be provided
        # Use 0 to get all available history
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": start_time if start_time is not None else 0,
            }
        }
        if end_time:
            payload["req"]["endTime"] = end_time

        data = await self._post(payload)

        bars = []
        for candle in data:
            bars.append(Bar(
                symbol=symbol,
                timeframe=TimeFrame(interval),
                timestamp=datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc),
                open=Decimal(str(candle["o"])),
                high=Decimal(str(candle["h"])),
                low=Decimal(str(candle["l"])),
                close=Decimal(str(candle["c"])),
                volume=Decimal(str(candle["v"])),
                trades_count=candle.get("n"),
            ))
        return bars

    # -------------------------------------------------------------------------
    # Funding
    # -------------------------------------------------------------------------
    async def get_funding_history(
        self,
        symbol: str,
        start_time: int,
        end_time: Optional[int] = None,
    ) -> List[Dict]:
        """Get historical funding rates."""
        payload = {
            "type": "fundingHistory",
            "coin": symbol,
            "startTime": start_time,
        }
        if end_time:
            payload["endTime"] = end_time
        return await self._post(payload)

    async def get_predicted_fundings(self) -> List[Dict]:
        """Get predicted funding rates."""
        return await self._post({"type": "predictedFundings"})

    # -------------------------------------------------------------------------
    # User State
    # -------------------------------------------------------------------------
    async def get_user_state(self, address: Optional[str] = None) -> Dict:
        """Get user account state."""
        addr = address or self.settings.hl_wallet_address
        return await self._post({
            "type": "clearinghouseState",
            "user": addr,
        })

    async def get_account_state(self, address: Optional[str] = None) -> AccountState:
        """Get parsed account state."""
        addr = address or self.settings.hl_wallet_address
        data = await self.get_user_state(addr)
        mids = await self.get_all_mids()

        # Parse margin summary
        margin = data.get("marginSummary", {})
        equity = Decimal(str(margin.get("accountValue", 0)))
        total_margin = Decimal(str(margin.get("totalMarginUsed", 0)))

        # Parse positions
        positions = []
        total_position_value = Decimal(0)
        total_unrealized_pnl = Decimal(0)

        for pos_data in data.get("assetPositions", []):
            pos = pos_data.get("position", {})
            coin = pos.get("coin", "")
            size = Decimal(str(pos.get("szi", 0)))

            if size == 0:
                continue

            entry_price = Decimal(str(pos.get("entryPx", 0)))
            current_price = mids.get(coin, entry_price)

            # Calculate unrealized P&L
            if size > 0:
                unrealized_pnl = (current_price - entry_price) * size
            else:
                unrealized_pnl = (entry_price - current_price) * abs(size)

            notional = abs(size) * current_price
            total_position_value += notional
            total_unrealized_pnl += unrealized_pnl

            # Get leverage info
            leverage_info = pos.get("leverage", {})
            leverage = Decimal(str(leverage_info.get("value", 1)))

            # Get liquidation price
            liq_price = pos.get("liquidationPx")
            if liq_price:
                liq_price = Decimal(str(liq_price))

            position = Position(
                symbol=coin,
                side=Side.LONG if size > 0 else Side.SHORT,
                size=abs(size),
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_pct=unrealized_pnl / (entry_price * abs(size)) if entry_price > 0 else Decimal(0),
                leverage=leverage,
                liquidation_price=liq_price,
                margin_used=Decimal(str(pos.get("marginUsed", 0))),
            )
            positions.append(position)

        # Calculate current leverage
        current_leverage = Decimal(0)
        if equity > 0:
            current_leverage = total_position_value / equity

        return AccountState(
            timestamp=datetime.now(timezone.utc),
            equity=equity,
            available_balance=equity - total_margin,
            total_margin_used=total_margin,
            positions=positions,
            total_unrealized_pnl=total_unrealized_pnl,
            total_position_value=total_position_value,
            current_leverage=current_leverage,
        )

    # -------------------------------------------------------------------------
    # Market Context
    # -------------------------------------------------------------------------
    async def get_market_context(self, symbol: str) -> MarketContext:
        """Get complete market context for a symbol."""
        # Get meta and asset contexts
        meta_ctxs = await self.get_meta_and_asset_ctxs()

        # Find asset context
        meta = meta_ctxs[0]
        asset_ctxs = meta_ctxs[1]

        asset_ctx = None
        for i, perp in enumerate(meta.get("universe", [])):
            if perp.get("name") == symbol:
                asset_ctx = asset_ctxs[i]
                break

        if not asset_ctx:
            raise DataFeedError(f"Symbol {symbol} not found", source="rest")

        # Get L2 book for depth
        book = await self.get_l2_book(symbol)
        bid_depth = sum(Decimal(str(level[1])) for level in book.get("levels", [[]])[0][:10])
        ask_depth = sum(Decimal(str(level[1])) for level in book.get("levels", [[]])[1][:10])

        return MarketContext(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            mark_price=Decimal(str(asset_ctx.get("markPx", 0))),
            index_price=Decimal(str(asset_ctx.get("oraclePx", 0))),
            mid_price=Decimal(str(asset_ctx.get("midPx", 0))),
            funding_rate=Decimal(str(asset_ctx.get("funding", 0))),
            open_interest=Decimal(str(asset_ctx.get("openInterest", 0))),
            volume_24h=Decimal(str(asset_ctx.get("dayNtlVlm", 0))),
            bid_depth=bid_depth,
            ask_depth=ask_depth,
        )

    async def get_all_market_contexts(self, symbols: List[str]) -> Dict[str, MarketContext]:
        """Get market context for multiple symbols efficiently."""
        meta_ctxs = await self.get_meta_and_asset_ctxs()
        meta = meta_ctxs[0]
        asset_ctxs = meta_ctxs[1]

        # Build symbol to index mapping
        symbol_to_idx = {}
        for i, perp in enumerate(meta.get("universe", [])):
            name = perp.get("name")
            if name in symbols:
                symbol_to_idx[name] = i

        contexts = {}
        for symbol in symbols:
            if symbol not in symbol_to_idx:
                continue

            idx = symbol_to_idx[symbol]
            ctx = asset_ctxs[idx]

            contexts[symbol] = MarketContext(
                symbol=symbol,
                timestamp=datetime.now(timezone.utc),
                mark_price=Decimal(str(ctx.get("markPx", 0))),
                index_price=Decimal(str(ctx.get("oraclePx", 0))),
                mid_price=Decimal(str(ctx.get("midPx", 0))),
                funding_rate=Decimal(str(ctx.get("funding", 0))),
                open_interest=Decimal(str(ctx.get("openInterest", 0))),
                volume_24h=Decimal(str(ctx.get("dayNtlVlm", 0))),
            )

        return contexts
