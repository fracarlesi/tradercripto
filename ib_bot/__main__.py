"""Allow running as: python -m ib_bot"""
import asyncio
from .main import main

if __name__ == "__main__":
    asyncio.run(main())
