#!/bin/bash
#
# Rate Limit Monitoring Script
# Checks all API usage against documented limits
#
# Usage:
#   ./monitor_rate_limits.sh              # Local logs
#   ./monitor_rate_limits.sh production   # Production VPS
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
PRODUCTION_IP="46.224.45.196"
MODE="${1:-local}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        API RATE LIMIT MONITORING - $(date +%Y-%m-%d)         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Mode: $MODE"
echo "Time window: Last 24 hours"
echo ""

# Helper function to get logs
get_logs() {
    local grep_pattern="$1"

    if [ "$MODE" = "production" ]; then
        ssh root@$PRODUCTION_IP "docker logs trader_bitcoin-app-1 --since 24h" | grep -c "$grep_pattern" || echo "0"
    else
        if [ -d "backend/logs" ]; then
            grep -r "$grep_pattern" backend/logs/*.log 2>/dev/null | wc -l || echo "0"
        else
            docker logs trader_bitcoin-app-1 --since 24h 2>/dev/null | grep -c "$grep_pattern" || echo "0"
        fi
    fi
}

# 1. Hyperliquid API - Check for 429 errors
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "1️⃣  HYPERLIQUID API (Trading Platform)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

hyperliquid_429=$(get_logs "429")
echo -n "   429 Rate Limit Errors: "
if [ "$hyperliquid_429" -eq 0 ]; then
    echo -e "${GREEN}$hyperliquid_429${NC} ✅ (Expected: 0)"
else
    echo -e "${RED}$hyperliquid_429${NC} 🚨 (ALERT: Should be 0!)"
fi

echo ""

# 2. DeepSeek API - Check for rate limiting
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "2️⃣  DEEPSEEK API (AI Decision Engine)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

deepseek_rate_limit=$(get_logs "DeepSeek.*rate limit")
echo -n "   Rate Limit Warnings: "
if [ "$deepseek_rate_limit" -eq 0 ]; then
    echo -e "${GREEN}$deepseek_rate_limit${NC} ✅ (Expected: 0)"
else
    echo -e "${YELLOW}$deepseek_rate_limit${NC} ⚠️  (Should be 0)"
fi

# Check token usage
deepseek_calls=$(get_logs "DeepSeek API response")
echo -n "   API Calls (24h): "
if [ "$deepseek_calls" -ge 400 ] && [ "$deepseek_calls" -le 500 ]; then
    echo -e "${GREEN}$deepseek_calls${NC} ✅ (Expected: ~480)"
elif [ "$deepseek_calls" -gt 500 ]; then
    echo -e "${YELLOW}$deepseek_calls${NC} ⚠️  (High usage)"
else
    echo -e "${YELLOW}$deepseek_calls${NC} ⚠️  (Low usage - system down?)"
fi

echo ""

# 3. CoinMarketCap API - Sentiment Index
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "3️⃣  COINMARKETCAP API (Sentiment Index)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sentiment_calls=$(get_logs "Fetching Fear & Greed Index")
echo -n "   API Calls (24h): "
if [ "$sentiment_calls" -le 30 ]; then
    echo -e "${GREEN}$sentiment_calls${NC} ✅ (Expected: ~24, Limit: 333/day)"
elif [ "$sentiment_calls" -le 100 ]; then
    echo -e "${YELLOW}$sentiment_calls${NC} ⚠️  (Higher than expected)"
else
    echo -e "${RED}$sentiment_calls${NC} 🚨 (ALERT: Too many calls!)"
fi

# Percentage of daily limit
if [ "$sentiment_calls" -gt 0 ]; then
    sentiment_pct=$((sentiment_calls * 100 / 333))
    echo "   Limit Usage: $sentiment_pct% of 333 calls/day"
fi

echo ""

# 4. Whale Alert API
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "4️⃣  WHALE ALERT API (Large Transactions)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

whale_calls=$(get_logs "whale")
echo -n "   API Calls (24h): "
if [ "$whale_calls" -le 50 ]; then
    echo -e "${GREEN}$whale_calls${NC} ✅ (Expected: <50, Monthly limit: 1000)"
elif [ "$whale_calls" -le 100 ]; then
    echo -e "${YELLOW}$whale_calls${NC} ⚠️  (Acceptable)"
else
    echo -e "${RED}$whale_calls${NC} 🚨 (ALERT: Too many calls!)"
fi

# Projected monthly usage
if [ "$whale_calls" -gt 0 ]; then
    whale_monthly=$((whale_calls * 30))
    echo "   Projected Monthly: $whale_monthly calls (Limit: 1,000)"
    if [ "$whale_monthly" -gt 1000 ]; then
        echo -e "   ${RED}⚠️  WARNING: Will exceed monthly limit!${NC}"
    fi
fi

echo ""

# 5. News API (CoinJournal)
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "5️⃣  NEWS API (CoinJournal)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

news_calls=$(get_logs "Fetching.*news\|fetch_latest_news")
echo -n "   API Calls (24h): "
if [ "$news_calls" -le 30 ]; then
    echo -e "${GREEN}$news_calls${NC} ✅ (Expected: ~24)"
elif [ "$news_calls" -le 100 ]; then
    echo -e "${YELLOW}$news_calls${NC} ⚠️  (Higher than expected)"
else
    echo -e "${RED}$news_calls${NC} 🚨 (ALERT: Too many calls!)"
fi

echo ""

# Summary
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Count issues
issues=0
[ "$hyperliquid_429" -gt 0 ] && issues=$((issues + 1))
[ "$deepseek_rate_limit" -gt 0 ] && issues=$((issues + 1))
[ "$sentiment_calls" -gt 100 ] && issues=$((issues + 1))
[ "$whale_calls" -gt 100 ] && issues=$((issues + 1))

if [ "$issues" -eq 0 ]; then
    echo -e "${GREEN}✅ ALL CLEAR - No rate limit issues detected${NC}"
elif [ "$issues" -eq 1 ]; then
    echo -e "${YELLOW}⚠️  1 ISSUE DETECTED - Review warnings above${NC}"
else
    echo -e "${RED}🚨 $issues ISSUES DETECTED - Immediate action required${NC}"
fi

echo ""
echo "Last updated: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
echo "For detailed analysis, see:"
echo "  backend/docs/API_RATE_LIMITS_ANALYSIS.md"
echo ""
