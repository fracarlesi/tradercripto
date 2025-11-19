#!/usr/bin/env python3
"""
Update Account to use DeepSeek via OpenRouter.

OpenRouter provides access to DeepSeek models with:
- base_url: https://openrouter.ai/api/v1
- model: deepseek/deepseek-chat (or deepseek/deepseek-reasoner)
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_path))

from sqlalchemy import select
from database.connection import init_async_engine
from database.models import Account


async def update_to_openrouter():
    """Update the DeepSeek account to use OpenRouter."""

    # OpenRouter configuration
    NEW_BASE_URL = "https://openrouter.ai/api/v1"
    NEW_API_KEY = "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"
    NEW_MODEL = "deepseek/deepseek-chat"  # OpenRouter model ID for DeepSeek

    # Initialize engine and session factory
    _, async_session_factory = init_async_engine()

    async with async_session_factory() as session:
        # Find all accounts (usually just one)
        result = await session.execute(select(Account))
        accounts = result.scalars().all()

        if not accounts:
            print("❌ No accounts found in database!")
            return

        for account in accounts:
            print(f"\n📋 Account: {account.name} (ID: {account.id})")
            print(f"   Old base_url: {account.base_url}")
            print(f"   Old model: {account.model}")
            print(f"   Old api_key: {account.api_key[:20] if account.api_key else 'None'}...")

            # Update configuration
            account.base_url = NEW_BASE_URL
            account.api_key = NEW_API_KEY
            account.model = NEW_MODEL

            print(f"\n   ✅ Updated to OpenRouter:")
            print(f"   New base_url: {NEW_BASE_URL}")
            print(f"   New model: {NEW_MODEL}")
            print(f"   New api_key: {NEW_API_KEY[:30]}...")

        await session.commit()
        print(f"\n✅ Successfully updated {len(accounts)} account(s) to use OpenRouter!")
        print("\n💡 DeepSeek is now accessible via OpenRouter API.")


if __name__ == "__main__":
    asyncio.run(update_to_openrouter())
