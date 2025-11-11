#!/usr/bin/env python3
"""
Test and Save JSON - Generate and save market snapshot JSON

Usage:
    cd backend/
    python3 scripts/testing/test_and_save_json.py
"""

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime

# Add backend to path
backend_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_path))

from services.orchestrator.market_data_orchestrator import build_market_data_snapshot


# Core symbols
SYMBOLS = ["BTC", "ETH", "SOL", "AVAX", "ARB"]


async def main():
    """Generate and save JSON snapshot."""
    print(f"Generating market snapshot for {len(SYMBOLS)} symbols...\n")

    # Generate snapshot
    snapshot = await build_market_data_snapshot(
        account_id=1,
        enable_prophet=False,
        prophet_mode=None,
        symbols_filter=SYMBOLS,
    )

    # Save to file
    output_dir = backend_path / "output"
    output_dir.mkdir(exist_ok=True)

    filename = f"market_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = output_dir / filename

    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    print(f"✅ JSON saved to: {filepath}")
    print(f"📊 File size: {filepath.stat().st_size / 1024:.1f} KB")

    # Display summary
    print(f"\n📋 Snapshot Summary:")
    print(f"  Symbols: {len(snapshot['symbols'])}")
    print(f"  Metadata: {snapshot['metadata']}")
    print(f"  Portfolio total: ${snapshot['portfolio']['total_assets']:.2f}")

    # Display top 3 by technical score
    top_symbols = sorted(
        snapshot["symbols"],
        key=lambda s: s["technical_analysis"]["score"],
        reverse=True,
    )[:3]

    print(f"\n🏆 Top 3 Technical Signals:")
    for i, s in enumerate(top_symbols, 1):
        ta = s["technical_analysis"]
        pivot = s.get("pivot_points", {})
        print(
            f"  {i}. {s['symbol']:6s} - "
            f"Score: {ta['score']:.3f} ({ta['signal']:12s}) - "
            f"Pivot: {pivot.get('current_zone', 'N/A')}"
        )

    print(f"\n📁 Full JSON available at: {filepath}")


if __name__ == "__main__":
    asyncio.run(main())
