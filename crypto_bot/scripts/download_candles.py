"""Download historical candles from Hyperliquid for FLAG-Trader training.

Usage:
    python -m scripts.download_candles --days 180 --interval 15m --min-volume 500000
"""

import argparse
import asyncio
import logging

from flag_trader.data_collector import HyperliquidDataCollector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Hyperliquid candles")
    parser.add_argument("--days", type=int, default=180, help="Days of history to fetch")
    parser.add_argument("--interval", type=str, default="15m", help="Candle interval (1m, 5m, 15m, 1h, 4h, 1d)")
    parser.add_argument("--min-volume", type=float, default=500_000, help="Min 24h USD volume filter")
    parser.add_argument("--symbols", nargs="*", help="Specific symbols to download (default: all above min-volume)")
    parser.add_argument("--data-dir", type=str, default="data/candles", help="Output directory for Parquet files")
    args = parser.parse_args()

    from pathlib import Path

    collector = HyperliquidDataCollector(data_dir=Path(args.data_dir))

    if args.symbols:
        for symbol in args.symbols:
            print(f"Downloading {symbol}...")
            df = asyncio.run(collector.fetch_candles(symbol, args.interval, args.days))
            print(f"  {len(df)} candles saved")
    else:
        print(f"Downloading all assets with 24h volume > ${args.min_volume:,.0f}...")
        results = asyncio.run(collector.fetch_all_assets(args.min_volume, args.interval, args.days))
        print(f"Downloaded {len(results)} assets")

    print(f"Data saved to {collector.data_dir}/")


if __name__ == "__main__":
    main()
