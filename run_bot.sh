#!/bin/bash
cd /opt/trader_bitcoin
echo "=== $(date '+%Y-%m-%d %H:%M:%S') - Starting bot run ===" >> /opt/trader_bitcoin/logs/bot.log
docker compose run --rm app python main.py >> /opt/trader_bitcoin/logs/bot.log 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') - Bot run completed ===" >> /opt/trader_bitcoin/logs/bot.log
