#!/usr/bin/env python3
"""
HLQuantBot Daemon Entry Point

This is the main entry point for running the bot in daemon mode.
"""

import asyncio
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from hlquantbot.bot import run_bot


def main():
    """Main entry point."""
    print("""
╔═══════════════════════════════════════════════════════════╗
║                     HLQuantBot v1.0                       ║
║           Quantitative Trading Bot for Hyperliquid        ║
╚═══════════════════════════════════════════════════════════╝
    """)

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nShutdown requested...")
    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
