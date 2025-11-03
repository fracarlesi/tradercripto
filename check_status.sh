#!/bin/bash
# Script di monitoraggio sistema trading
# Esegui questo script quando torni per verificare cosa è successo

echo "═══════════════════════════════════════════════════════════════"
echo "📊 REPORT SISTEMA TRADING"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# 1. Verifica processi attivi
echo "🔧 PROCESSI ATTIVI:"
echo ""
echo "Backend (porta 5611):"
ps aux | grep "[u]vicorn.*5611" && echo "  ✅ Backend attivo" || echo "  ❌ Backend NON attivo"

echo ""
echo "Frontend (porta 5621):"
ps aux | grep "[v]ite.*5621" && echo "  ✅ Frontend attivo" || echo "  ❌ Frontend NON attivo"

echo ""
echo "═══════════════════════════════════════════════════════════════"

# 2. Check decisioni AI nel database
echo "🤖 DECISIONI AI (ultime 10):"
echo ""
cd backend
sqlite3 data.db <<EOF
.mode column
.headers on
SELECT
  id,
  datetime(decision_time, 'unixepoch') as time,
  operation,
  symbol,
  target_portion,
  executed
FROM ai_decision_logs
ORDER BY id DESC
LIMIT 10;
EOF

if [ $? -ne 0 ]; then
  echo "⚠️  Nessuna decisione trovata o errore database"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"

# 3. Balance attuale
echo "💰 BALANCE ATTUALE:"
echo ""
sqlite3 data.db <<EOF
SELECT
  name,
  printf('$%.2f', current_cash) as balance,
  is_active
FROM accounts
WHERE account_type = 'AI';
EOF

echo ""
echo "═══════════════════════════════════════════════════════════════"

# 4. Ultimi 30 log del backend
echo "📝 ULTIMI LOG BACKEND (ultimi 30):"
echo ""
tail -30 /tmp/trader_app.log | grep -E "AI Trading|Decision|executed|error|ERROR" || echo "Nessun log AI trading trovato"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "✅ Report completato"
echo ""
echo "Per vedere tutti i log: tail -f /tmp/trader_app.log"
echo "Per vedere frontend: apri http://localhost:5621"
echo "═══════════════════════════════════════════════════════════════"
