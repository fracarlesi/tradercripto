"""
Download historical daily data for S&P 500 stocks via yfinance.

Usage:
    python3 -m ib_bot.scripts.download_training_data --symbols all --days 730
    python3 -m ib_bot.scripts.download_training_data --top-n 100 --days 730
    python3 -m ib_bot.scripts.download_training_data --symbols AAPL,MSFT,GOOG --days 365
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from tqdm import tqdm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S&P 500 top 100 by market cap (as of early 2026, approximate)
# Full S&P 500 list can be fetched dynamically or imported from universe.py
# ---------------------------------------------------------------------------
SP500_TOP100: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK-B",
    "LLY", "AVGO", "JPM", "TSLA", "UNH", "XOM", "V", "MA", "PG",
    "COST", "JNJ", "HD", "ABBV", "WMT", "NFLX", "BAC", "CRM", "ORCL",
    "CVX", "MRK", "KO", "AMD", "PEP", "ADBE", "TMO", "ACN", "LIN",
    "MCD", "CSCO", "ABT", "WFC", "PM", "DHR", "NOW", "QCOM", "GE",
    "TXN", "INTU", "CAT", "ISRG", "VZ", "AMGN", "CMCSA", "IBM",
    "AMAT", "MS", "PFE", "DIS", "GS", "NEE", "AXP", "HON", "UNP",
    "LOW", "RTX", "BKNG", "SYK", "SPGI", "BLK", "UBER", "T", "COP",
    "ELV", "PLD", "MDLZ", "SCHW", "DE", "LRCX", "VRTX", "BMY",
    "PANW", "ADP", "CB", "GILD", "KLAC", "ADI", "SBUX", "TMUS",
    "FI", "SO", "MU", "CI", "BSX", "MMC", "DUK", "SHW", "ICE",
    "CME", "REGN", "PGR", "CL", "SNPS",
]

# Full S&P 500 list - attempts dynamic fetch, falls back to top 100
SP500_FULL: list[str] | None = None


def _get_sp500_full() -> list[str]:
    """Try to get full S&P 500 list from Wikipedia, fall back to top 100."""
    global SP500_FULL
    if SP500_FULL is not None:
        return SP500_FULL

    # Try importing from scanner/universe.py if available
    try:
        from ib_bot.scanner.universe import SP500_SYMBOLS  # type: ignore[import-not-found]
        SP500_FULL = list(SP500_SYMBOLS)
        logger.info("Loaded %d symbols from ib_bot.scanner.universe", len(SP500_FULL))
        return SP500_FULL
    except ImportError:
        pass

    # Try fetching from Wikipedia
    try:
        table = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            header=0,
        )
        symbols = table[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        SP500_FULL = sorted(set(symbols))
        logger.info("Fetched %d S&P 500 symbols from Wikipedia", len(SP500_FULL))
        return SP500_FULL
    except Exception as e:
        logger.warning("Failed to fetch S&P 500 list from Wikipedia: %s", e)
        logger.info("Falling back to built-in top 100 list")
        SP500_FULL = SP500_TOP100
        return SP500_FULL


def download_symbol(
    symbol: str,
    start_date: str,
    end_date: str,
    output_dir: Path,
) -> bool:
    """Download daily OHLCV data for a single symbol.

    Returns True if data was saved, False on error.
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date, end=end_date, interval="1d", auto_adjust=True)

        if df.empty:
            logger.warning("No data returned for %s", symbol)
            return False

        # Standardise columns
        df = df.reset_index()
        df = df.rename(columns={
            "Date": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        df = df[["date", "open", "high", "low", "close", "volume"]]

        # Ensure date is date-only (no timezone)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()

        # Drop rows with NaN prices
        df = df.dropna(subset=["open", "high", "low", "close"])

        if len(df) < 10:
            logger.warning("Insufficient data for %s (%d rows)", symbol, len(df))
            return False

        output_path = output_dir / f"{symbol}.parquet"
        df.to_parquet(output_path, index=False)
        return True

    except Exception as e:
        logger.error("Error downloading %s: %s", symbol, e)
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Download S&P 500 daily OHLCV data for LLM-Equity training.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m ib_bot.scripts.download_training_data --symbols all --days 730
  python3 -m ib_bot.scripts.download_training_data --top-n 100
  python3 -m ib_bot.scripts.download_training_data --symbols AAPL,MSFT,GOOG --days 365
        """,
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="all",
        help='Comma-separated symbols or "all" for full S&P 500 (default: all)',
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Download only top N symbols by market cap (uses built-in ranked list)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=730,
        help="Number of days of history to download (default: 730)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="ib_bot/data/training/equity_daily",
        help="Output directory for Parquet files (default: ib_bot/data/training/equity_daily)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Resolve symbol list
    if args.top_n is not None:
        symbols = SP500_TOP100[: args.top_n]
        logger.info("Using top %d symbols by market cap", len(symbols))
    elif args.symbols.lower() == "all":
        symbols = _get_sp500_full()
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if not symbols:
        logger.error("No symbols to download")
        sys.exit(1)

    # Date range
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    logger.info(
        "Downloading %d symbols, period %s to %s", len(symbols), start_date, end_date
    )

    # Output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Download with progress bar
    success = 0
    failed: list[str] = []

    for symbol in tqdm(symbols, desc="Downloading", unit="symbol"):
        if download_symbol(symbol, start_date, end_date, output_dir):
            success += 1
        else:
            failed.append(symbol)

    # Summary
    logger.info("=" * 50)
    logger.info("Download complete: %d/%d successful", success, len(symbols))
    if failed:
        logger.warning("Failed symbols (%d): %s", len(failed), ", ".join(failed))
    logger.info("Output directory: %s", output_dir.resolve())


if __name__ == "__main__":
    main()
