#!/usr/bin/env python3
"""
Quick test of TOON encoder for LLM token savings.

Usage:
    cd backend/
    python3 scripts/testing/test_toon_encoder.py
"""

import sys
from pathlib import Path
import json

# Add backend to path
backend_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_path))

from services.toon_encoder import encode as toon_encode, estimate_token_savings


def test_toon_encoder():
    """Test TOON encoding with sample trading data"""

    print("=" * 80)
    print("TOON ENCODER TEST - Token Savings for LLM Communication")
    print("=" * 80)
    print()

    # Sample data similar to what AI service uses
    sample_data = {
        "prices": [
            {"symbol": "BTC", "price": 104457.5},
            {"symbol": "ETH", "price": 3555.85},
            {"symbol": "SOL", "price": 163.345},
            {"symbol": "AVAX", "price": 17.7495},
            {"symbol": "ARB", "price": 0.2909},
        ],
        "technical_recommendations": [
            {"symbol": "BTC", "score": 0.553, "momentum": 0.106, "support": 1.0, "signal": "HOLD"},
            {"symbol": "ETH", "score": 0.220, "momentum": 0.058, "support": 0.382, "signal": "STRONG_SELL"},
            {"symbol": "SOL", "score": 0.071, "momentum": 0.040, "support": 0.101, "signal": "STRONG_SELL"},
        ],
        "positions": [
            {"symbol": "BTC", "qty": 0.5, "avg_cost": 100000, "current_price": 104457.5, "profit_pct": 4.46}
        ],
    }

    # Encode to JSON (traditional)
    json_output = json.dumps(sample_data, indent=2)

    # Encode to TOON (optimized)
    toon_output = toon_encode(sample_data)

    # Calculate savings
    savings = estimate_token_savings(json_output, toon_output)

    print("SAMPLE DATA (5 symbols):")
    print("-" * 80)
    print()

    print("JSON FORMAT (traditional):")
    print("-" * 40)
    print(json_output[:500] + "..." if len(json_output) > 500 else json_output)
    print()
    print(f"Total characters: {savings['json_chars']}")
    print(f"Estimated tokens: {savings['json_tokens_estimate']}")
    print()

    print("TOON FORMAT (optimized for LLM):")
    print("-" * 40)
    print(toon_output[:500] + "..." if len(toon_output) > 500 else toon_output)
    print()
    print(f"Total characters: {savings['toon_chars']}")
    print(f"Estimated tokens: {savings['toon_tokens_estimate']}")
    print()

    print("=" * 80)
    print("TOKEN SAVINGS")
    print("=" * 80)
    print(f"  Savings: {savings['savings_pct']}%")
    print(f"  Tokens saved: {savings['savings_tokens']}")
    print(f"  JSON tokens: {savings['json_tokens_estimate']}")
    print(f"  TOON tokens: {savings['toon_tokens_estimate']}")
    print()

    # Extrapolate to full production scale (222 prices + 171 technical recommendations)
    full_scale_factor = (222 + 171) / len(sample_data["prices"] + sample_data["technical_recommendations"])
    estimated_full_savings = int(savings['savings_tokens'] * full_scale_factor)

    print("=" * 80)
    print("ESTIMATED PRODUCTION SAVINGS (222 prices + 171 technical symbols)")
    print("=" * 80)
    print(f"  Estimated tokens saved per AI call: ~{estimated_full_savings}")
    print(f"  AI calls per day (10-min interval): {24 * 6} = 144")
    print(f"  Total tokens saved per day: ~{estimated_full_savings * 144:,}")
    print(f"  Total tokens saved per month: ~{estimated_full_savings * 144 * 30:,}")
    print()

    # Cost calculation (assuming $1 per 1M tokens for DeepSeek)
    cost_per_token = 0.14 / 1_000_000  # DeepSeek input: $0.14/1M tokens
    daily_savings = (estimated_full_savings * 144) * cost_per_token
    monthly_savings = daily_savings * 30

    print(f"  Cost savings (DeepSeek $0.14/1M input tokens):")
    print(f"    Per day: ${daily_savings:.2f}")
    print(f"    Per month: ${monthly_savings:.2f}")
    print()

    if savings['savings_pct'] >= 30:
        print("✅ TOON encoder working correctly!")
        print(f"   Achieved {savings['savings_pct']}% token savings (target: 30-60%)")
        return True
    else:
        print(f"⚠️  Warning: Only {savings['savings_pct']}% savings (expected 30-60%)")
        return False


if __name__ == "__main__":
    success = test_toon_encoder()
    sys.exit(0 if success else 1)
