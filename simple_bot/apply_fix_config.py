#!/usr/bin/env python3
"""
Apply Fixed Configuration
=========================
Inserts a new parameter version with conservative settings to reduce losses.

Run: python simple_bot/apply_fix_config.py
"""

import asyncio
import asyncpg
import os
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://trader:trader_password@localhost:5432/trading_db"
)

# New conservative configuration
NEW_CONFIG = {
    "tp_pct": 0.015,           # 1.5% (was 0.8%)
    "sl_pct": 0.01,            # 1.0% (was 0.4%)
    "position_size_usd": 50.0, # $50 (was $50 - keep)
    "leverage": 3,             # 3x (was 3x - keep)
    
    # Momentum
    "momentum_enabled": True,
    "momentum_ema_fast": 12,      # Faster
    "momentum_ema_slow": 26,      # Standard MACD
    "momentum_rsi_period": 14,
    "momentum_rsi_long": 55,
    "momentum_rsi_short": 45,
    
    # Mean Reversion - MORE CONSERVATIVE
    "meanrev_enabled": True,
    "meanrev_rsi_oversold": 25,   # More extreme (was 35)
    "meanrev_rsi_overbought": 75, # More extreme (was 65)
    "meanrev_bb_period": 20,
    "meanrev_bb_std": 2.0,        # Standard (was 1.5)
    
    # Breakout - MORE CONSERVATIVE
    "breakout_enabled": True,
    "breakout_lookback": 30,       # Longer (was 20)
    "breakout_min_pct": 0.005,     # 0.5% (was 0.15%)
}

REASONING = """
## Fix Applied - Conservative Parameters

### Changes Made:
1. **TP/SL Ratio Fixed**: Changed from 0.8%/0.4% (2:1 against us) to 1.5%/1.0% (1.5:1 in our favor)
2. **Mean Reversion**: RSI thresholds changed from 35/65 to 25/75 - fewer but higher quality signals
3. **Bollinger Bands**: Changed from 1.5 std to 2.0 std - standard setting for fewer false signals  
4. **Breakout**: Minimum breakout increased from 0.15% to 0.5% - filters noise
5. **Breakout Lookback**: Increased from 20 to 30 bars for stronger breakouts

### Expected Improvements:
- Fewer trades but higher quality
- Better risk/reward ratio
- Reduced losses from false signals
- Less susceptible to market noise
"""


async def apply_config():
    """Apply the new configuration to the database."""
    print("Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        print("Current active configuration:")
        current = await conn.fetchrow("""
            SELECT version_id, source, tp_pct, sl_pct, 
                   meanrev_rsi_oversold, meanrev_rsi_overbought,
                   breakout_min_pct
            FROM parameter_versions 
            WHERE is_active = TRUE
        """)
        if current:
            print(f"  Version: {current['version_id']}")
            print(f"  Source: {current['source']}")
            print(f"  TP/SL: {float(current['tp_pct'])*100:.2f}% / {float(current['sl_pct'])*100:.2f}%")
            print(f"  MeanRev RSI: {current['meanrev_rsi_oversold']}/{current['meanrev_rsi_overbought']}")
            print(f"  Breakout min: {float(current['breakout_min_pct'])*100:.3f}%")
        else:
            print("  No active configuration found!")
        
        print("\nApplying new conservative configuration...")
        
        # Deactivate current version
        await conn.execute("""
            UPDATE parameter_versions SET is_active = FALSE WHERE is_active = TRUE
        """)
        
        # Insert new version
        version_id = await conn.fetchval("""
            INSERT INTO parameter_versions (
                source, llm_reasoning,
                tp_pct, sl_pct, position_size_usd, leverage,
                momentum_enabled, momentum_ema_fast, momentum_ema_slow,
                momentum_rsi_period, momentum_rsi_long, momentum_rsi_short,
                meanrev_enabled, meanrev_rsi_oversold, meanrev_rsi_overbought,
                meanrev_bb_period, meanrev_bb_std,
                breakout_enabled, breakout_lookback, breakout_min_pct,
                is_active, applied_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19, $20, TRUE, NOW()
            ) RETURNING version_id
        """,
            "manual",  # source
            REASONING,
            NEW_CONFIG["tp_pct"],
            NEW_CONFIG["sl_pct"],
            NEW_CONFIG["position_size_usd"],
            NEW_CONFIG["leverage"],
            NEW_CONFIG["momentum_enabled"],
            NEW_CONFIG["momentum_ema_fast"],
            NEW_CONFIG["momentum_ema_slow"],
            NEW_CONFIG["momentum_rsi_period"],
            NEW_CONFIG["momentum_rsi_long"],
            NEW_CONFIG["momentum_rsi_short"],
            NEW_CONFIG["meanrev_enabled"],
            NEW_CONFIG["meanrev_rsi_oversold"],
            NEW_CONFIG["meanrev_rsi_overbought"],
            NEW_CONFIG["meanrev_bb_period"],
            NEW_CONFIG["meanrev_bb_std"],
            NEW_CONFIG["breakout_enabled"],
            NEW_CONFIG["breakout_lookback"],
            NEW_CONFIG["breakout_min_pct"],
        )
        
        print(f"\n✅ New configuration applied!")
        print(f"   Version ID: {version_id}")
        print(f"   TP/SL: {NEW_CONFIG['tp_pct']*100:.2f}% / {NEW_CONFIG['sl_pct']*100:.2f}%")
        print(f"   MeanRev RSI: {NEW_CONFIG['meanrev_rsi_oversold']}/{NEW_CONFIG['meanrev_rsi_overbought']}")
        print(f"   Breakout min: {NEW_CONFIG['breakout_min_pct']*100:.3f}%")
        print(f"\n⚠️  The bot will pick up this configuration on the next cycle.")
        print(f"   If running with hot-reload, changes take effect immediately.")
        
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(apply_config())
