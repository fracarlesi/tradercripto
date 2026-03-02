"""Hyperliquid API client for fetching assets and candle data."""

from __future__ import annotations

import time

import requests

API_URL = "https://api.hyperliquid.xyz/info"
RATE_LIMIT_SLEEP = 0.25


def get_all_assets_with_info() -> tuple[list[str], dict[str, int]]:
    """Fetch all tradeable asset symbols and their max leverage from Hyperliquid."""
    resp = requests.post(API_URL, json={"type": "meta"}, timeout=10)
    resp.raise_for_status()
    universe = resp.json()["universe"]
    names = [u["name"] for u in universe]
    leverage_caps = {u["name"]: u.get("maxLeverage", 100) for u in universe}
    return names, leverage_caps


def get_all_assets() -> list[str]:
    """Fetch all tradeable asset symbols from Hyperliquid."""
    names, _ = get_all_assets_with_info()
    return names


def get_candles(asset: str, interval: str, start_ms: int,
                end_ms: int) -> list[dict]:
    """Fetch OHLCV candles with retry on 429."""
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": asset, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    }
    for attempt in range(3):
        resp = requests.post(API_URL, json=payload, timeout=15)
        if resp.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        resp.raise_for_status()
        break
    else:
        resp.raise_for_status()
    candles = [
        {"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
         "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])}
        for c in resp.json()
    ]
    candles.sort(key=lambda x: x["t"])
    return candles


def fetch_all_candles(
    assets: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    exclude: set[str],
    warmup_bars: int,
) -> tuple[dict[str, list[dict]], int, int]:
    """Fetch candles for all assets, filtering exclusions and short series.

    Returns: (asset_candles, errors, skipped)
    """
    filtered = [a for a in assets if a not in exclude]
    print(f"Found {len(assets)} total, {len(filtered)} after exclusions")
    print()
    print("Fetching candle data...")

    asset_candles: dict[str, list[dict]] = {}
    errors = skipped = 0

    for idx, asset in enumerate(filtered):
        if (idx + 1) % 30 == 0 or idx == 0:
            print(f"  [{idx + 1}/{len(filtered)}] {asset}...")
        try:
            candles = get_candles(asset, interval, start_ms, end_ms)
        except Exception:
            errors += 1
            time.sleep(RATE_LIMIT_SLEEP)
            continue
        time.sleep(RATE_LIMIT_SLEEP)

        if len(candles) < warmup_bars:
            skipped += 1
            continue

        asset_candles[asset] = candles

    print(f"\nLoaded {len(asset_candles)} assets "
          f"({errors} errors, {skipped} skipped < {warmup_bars} bars)")
    print()
    return asset_candles, errors, skipped
