"""
Check IB Connection
====================

Diagnostic script to verify IB TWS/Gateway connectivity.

Usage: python -m ib_bot.scripts.check_ib_connection
"""

import asyncio
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ib_bot.config.loader import load_config


async def main() -> None:
    print("=" * 50)
    print("IB Connection Check")
    print("=" * 50)

    config = load_config()
    print(f"\nConfig loaded:")
    print(f"  Host: {config.ib_connection.host}")
    print(f"  Port: {config.ib_connection.port}")
    print(f"  Client ID: {config.ib_connection.client_id}")
    print(f"  Readonly: {config.ib_connection.readonly}")

    try:
        from ib_insync import IB

        ib = IB()
        print(f"\nConnecting to {config.ib_connection.host}:{config.ib_connection.port}...")

        await ib.connectAsync(
            host=config.ib_connection.host,
            port=config.ib_connection.port,
            clientId=config.ib_connection.client_id,
            timeout=10,
            readonly=True,
        )

        print("Connected!")
        print(f"  Server version: {ib.client.serverVersion()}")
        print(f"  Accounts: {ib.managedAccounts()}")

        # Check positions
        positions = ib.positions()
        print(f"  Positions: {len(positions)}")

        ib.disconnect()
        print("\nDisconnected. Connection check PASSED.")

    except ImportError:
        print("\nERROR: ib_insync not installed.")
        print("Install with: pip install ib_insync")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nTroubleshooting:")
        print("  1. Is TWS or IB Gateway running?")
        print("  2. Is the API enabled? (TWS: File > Global Config > API > Settings)")
        print("  3. Is the port correct? (TWS paper: 7497, live: 7496)")
        print("  4. Is the client ID already in use?")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
