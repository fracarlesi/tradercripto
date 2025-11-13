#!/usr/bin/env python3
"""
Automated migration script: Daily → Hourly Momentum Trading

This script converts the trading system from:
- Daily candles (1d, 71 periods) + Prophet forecasting
To:
- Hourly candles (1h, 24 periods) + Momentum-based trading

Changes:
1. Remove Prophet forecasting completely
2. Change candle period from 1d to 1h
3. Change candle limit from 71 to 24
4. Add hourly momentum calculation
5. Reduce AI cycle from 10min to 3min
6. Update AI prompt for momentum focus
"""

import re
import sys
from pathlib import Path

# Add backend to path
BACKEND_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def migrate_file(file_path: Path, changes: list) -> bool:
    """Apply list of changes to a file."""
    try:
        content = file_path.read_text()
        original = content

        for pattern, replacement in changes:
            if isinstance(pattern, str):
                content = content.replace(pattern, replacement)
            else:  # regex
                content = pattern.sub(replacement, content)

        if content != original:
            file_path.write_text(content)
            print(f"✅ Modified: {file_path.relative_to(BACKEND_DIR)}")
            return True
        else:
            print(f"⏭️  Skipped (no changes): {file_path.relative_to(BACKEND_DIR)}")
            return False

    except Exception as e:
        print(f"❌ Error migrating {file_path}: {e}")
        return False


def main():
    print("=" * 80)
    print("MIGRATION: Daily → Hourly Momentum Trading")
    print("=" * 80)

    modified_count = 0

    # 1. Remove Prophet from orchestrator
    print("\n📝 Step 1: Remove Prophet from market_data_orchestrator.py")
    orchestrator_file = BACKEND_DIR / "services/orchestrator/market_data_orchestrator.py"

    changes = [
        # Remove Prophet import
        (re.compile(r'from services\.market_data\.prophet_forecaster import.*\n'), ''),
        # Remove Prophet instantiation
        (re.compile(r'self\.prophet_forecaster = .*\n'), ''),
        # Remove Prophet forecast call
        (re.compile(r'\s+# Run Prophet.*\n.*forecast_result = await.*\n.*\n', re.DOTALL), ''),
        # Remove prophet_trend from data dict
        ('"prophet_trend": forecast_result.get("trend")', '# Prophet removed - using hourly momentum instead'),
    ]

    if migrate_file(orchestrator_file, changes):
        modified_count += 1

    # 2. Change candle period 1d → 1h and limit 71 → 24
    print("\n📝 Step 2: Change candle period to hourly (1d → 1h, 71 → 24)")

    market_data_file = BACKEND_DIR / "services/market_data/hyperliquid_market_data.py"

    changes = [
        ('period="1d"', 'period="1h"'),
        ('limit=71', 'limit=24'),
        (' 71 daily', ' 24 hourly'),
        (' 71 days', ' 24 hours'),
    ]

    if migrate_file(market_data_file, changes):
        modified_count += 1

    # 3. Update AI cycle interval 600s → 180s
    print("\n📝 Step 3: Reduce AI cycle interval (10min → 3min)")

    main_file = BACKEND_DIR / "main.py"

    changes = [
        ('interval_seconds=600,  # 10 minutes', 'interval_seconds=180,  # 3 minutes - Fast momentum trading'),
        ('# 10-minute interval prevents', '# 3-minute interval for fast momentum capture'),
    ]

    if migrate_file(main_file, changes):
        modified_count += 1

    # 4. Remove prophet dependency from requirements
    print("\n📝 Step 4: Remove Prophet from dependencies")

    requirements_file = BACKEND_DIR / "requirements.txt"

    changes = [
        (re.compile(r'prophet.*\n'), ''),
        (re.compile(r'fbprophet.*\n'), ''),
    ]

    if migrate_file(requirements_file, changes):
        modified_count += 1

    pyproject_file = BACKEND_DIR / "pyproject.toml"

    changes = [
        (re.compile(r'"prophet.*",?\n'), ''),
        (re.compile(r'"fbprophet.*",?\n'), ''),
    ]

    if migrate_file(pyproject_file, changes):
        modified_count += 1

    # 5. Update json_builder to remove prophet_trend
    print("\n📝 Step 5: Remove prophet_trend from JSON builder")

    json_builder_file = BACKEND_DIR / "services/orchestrator/json_builder.py"

    changes = [
        ('"prophet_trend":', '# "prophet_trend": # Removed - using hourly momentum'),
        ('symbol_data.get("prophet_trend")', 'None  # Prophet removed'),
    ]

    if migrate_file(json_builder_file, changes):
        modified_count += 1

    print("\n" + "=" * 80)
    print(f"✅ Migration complete! Modified {modified_count} files")
    print("=" * 80)

    print("\n🔧 Next manual steps:")
    print("1. Add hourly momentum calculation service")
    print("2. Update AI prompt to focus on hourly momentum")
    print("3. Test the new system")
    print("4. Deploy to production")


if __name__ == "__main__":
    main()
