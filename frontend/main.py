from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

import httpx
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


# Carica variabili d'ambiente da .env (se presente)
load_dotenv()


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL non impostata. Imposta la variabile d'ambiente, "
        "ad esempio: postgresql://user:password@localhost:5432/trading_db",
    )


@contextmanager
def get_connection():
    """Context manager che restituisce una connessione PostgreSQL.

    Usa il DSN in DATABASE_URL.
    """

    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


# =====================
# Modelli di risposta API
# =====================


class BalancePoint(BaseModel):
    timestamp: datetime
    balance_usd: float


class OpenPosition(BaseModel):
    id: int
    snapshot_id: int
    symbol: str
    side: str
    size: float
    entry_price: Optional[float]
    mark_price: Optional[float]
    pnl_usd: Optional[float]
    leverage: Optional[str]
    snapshot_created_at: datetime


class BotOperation(BaseModel):
    id: int
    created_at: datetime
    operation: str
    symbol: Optional[str]
    direction: Optional[str]
    target_portion_of_balance: Optional[float]
    leverage: Optional[float]
    raw_payload: Any
    system_prompt: Optional[str]


class RegimeDecision(BaseModel):
    id: int
    timestamp: datetime
    regime: str
    confidence: float
    risk_adjustment: float
    asset_regimes: Any
    analysis: Optional[str]


class SymbolPerformance(BaseModel):
    symbol: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    gross_profit: float
    gross_loss: float
    is_blacklisted: bool = False


# =====================
# App FastAPI + Template Jinja2
# =====================


app = FastAPI(
    title="Trading Agent Dashboard API",
    description=(
        "API per leggere i dati del trading agent dal database Postgres: "
        "saldo nel tempo, posizioni aperte, operazioni del bot con full prompt."
    ),
    version="0.3.1",
)

templates = Jinja2Templates(directory="templates")

# Timezone Italia
ROME_TZ = ZoneInfo("Europe/Rome")
UTC_TZ = ZoneInfo("UTC")


def to_rome_tz(dt):
    """Converte datetime UTC in ora italiana."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(ROME_TZ).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["to_rome"] = to_rome_tz


# =====================
# Endpoint API JSON
# =====================


@app.get("/balance", response_model=List[BalancePoint])
def get_balance() -> List[BalancePoint]:
    """Restituisce TUTTA la storia del saldo (equity) ordinata nel tempo.

    I dati sono presi dalla tabella `account_snapshots`.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, equity
                FROM account_snapshots
                ORDER BY timestamp ASC;
                """
            )
            rows = cur.fetchall()

    return [
        BalancePoint(timestamp=row[0], balance_usd=float(row[1]))
        for row in rows
    ]


@app.get("/open-positions", response_model=List[OpenPosition])
def get_open_positions() -> List[OpenPosition]:
    """Restituisce le posizioni aperte più recenti.

    - Recupera le posizioni dalla tabella `positions`.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Posizioni più recenti (ultima snapshot_time)
            cur.execute(
                """
                SELECT
                    id,
                    0 as snapshot_id,
                    symbol,
                    side,
                    size,
                    entry_price,
                    current_price,
                    unrealized_pnl,
                    leverage,
                    snapshot_time
                FROM positions
                WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM positions)
                ORDER BY symbol ASC, id ASC;
                """
            )
            rows = cur.fetchall()

    return [
        OpenPosition(
            id=row[0],
            snapshot_id=row[1],
            symbol=row[2],
            side=row[3],
            size=float(row[4]),
            entry_price=float(row[5]) if row[5] is not None else None,
            mark_price=float(row[6]) if row[6] is not None else None,
            pnl_usd=float(row[7]) if row[7] is not None else None,
            leverage=str(row[8]) if row[8] is not None else None,
            snapshot_created_at=row[9],
        )
        for row in rows
    ]


@app.get("/bot-operations", response_model=List[BotOperation])
def get_bot_operations(
    limit: int = Query(
        50,
        ge=1,
        le=500,
        description="Numero massimo di operazioni da restituire (default 50)",
    ),
) -> List[BotOperation]:
    """Restituisce le ULTIME `limit` operazioni (trades) del bot.

    - I dati provengono dalla tabella `trades`.
    - Ordinati da più recente a meno recente.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    entry_time,
                    CASE WHEN exit_time IS NULL THEN 'OPEN' ELSE 'CLOSED' END as operation,
                    symbol,
                    side,
                    size,
                    entry_price,
                    metadata,
                    strategy_id
                FROM trades
                ORDER BY entry_time DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()

    operations: List[BotOperation] = []
    for row in rows:
        operations.append(
            BotOperation(
                id=row[0],
                created_at=row[1],
                operation=row[2],
                symbol=row[3],
                direction=row[4],
                target_portion_of_balance=float(row[5]) if row[5] is not None else None,
                leverage=float(row[6]) if row[6] is not None else None,
                raw_payload=row[7],
                system_prompt=row[8],
            )
        )

    return operations


@app.get("/regime-history", response_model=List[RegimeDecision])
def get_regime_history(
    limit: int = Query(
        50,
        ge=1,
        le=500,
        description="Numero massimo di decisioni da restituire (default 50)",
    ),
) -> List[RegimeDecision]:
    """Restituisce le ultime analisi del regime di mercato."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    timestamp,
                    regime,
                    confidence,
                    risk_adjustment,
                    asset_regimes,
                    analysis
                FROM regime_history
                ORDER BY timestamp DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return [
        RegimeDecision(
            id=row[0],
            timestamp=row[1],
            regime=row[2],
            confidence=float(row[3]) if row[3] is not None else 0,
            risk_adjustment=float(row[4]) if row[4] is not None else 0,
            asset_regimes=row[5],
            analysis=row[6],
        )
        for row in rows
    ]


@app.get("/pnl-by-symbol", response_model=List[SymbolPerformance])
def get_pnl_by_symbol(
    lookback_hours: int = Query(
        24,
        ge=1,
        le=168,
        description="Ore di lookback per il calcolo P&L (default 24h, max 168h/7gg)",
    ),
) -> List[SymbolPerformance]:
    """Restituisce P&L breakdown per simbolo (ultimi N ore)."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    symbol,
                    COUNT(*) as total_trades,
                    COUNT(*) FILTER (WHERE pnl > 0) as winning_trades,
                    COUNT(*) FILTER (WHERE pnl <= 0) as losing_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as gross_profit,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN pnl ELSE 0 END), 0) as gross_loss
                FROM trades
                WHERE exit_time IS NOT NULL
                  AND exit_time >= NOW() - INTERVAL '%s hours'
                GROUP BY symbol
                ORDER BY total_pnl DESC;
                """ % lookback_hours,
            )
            rows = cur.fetchall()

    results = []
    for row in rows:
        total = row[1] if row[1] else 0
        winning = row[2] if row[2] else 0
        win_rate = (winning / total * 100) if total > 0 else 0

        results.append(
            SymbolPerformance(
                symbol=row[0],
                total_trades=total,
                winning_trades=winning,
                losing_trades=row[3] if row[3] else 0,
                win_rate=win_rate,
                total_pnl=float(row[4]) if row[4] else 0,
                avg_pnl=float(row[5]) if row[5] else 0,
                gross_profit=float(row[6]) if row[6] else 0,
                gross_loss=float(row[7]) if row[7] else 0,
                is_blacklisted=(win_rate < 40 and total >= 10),
            )
        )

    return results


# =====================
# Endpoint HTML + HTMX
# =====================


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard principale HTML."""

    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/ui/balance", response_class=HTMLResponse)
async def ui_balance(request: Request) -> HTMLResponse:
    """Partial HTML con il grafico del saldo nel tempo."""

    points = get_balance()
    labels = [p.timestamp.isoformat() for p in points]
    values = [p.balance_usd for p in points]
    return templates.TemplateResponse(
        "partials/balance_table.html",
        {"request": request, "labels": labels, "values": values},
    )


@app.get("/ui/open-positions", response_class=HTMLResponse)
async def ui_open_positions(request: Request) -> HTMLResponse:
    """Partial HTML con le posizioni aperte (ultimo snapshot)."""

    positions = get_open_positions()
    return templates.TemplateResponse(
        "partials/open_positions_table.html",
        {"request": request, "positions": positions},
    )


@app.get("/ui/bot-operations", response_class=HTMLResponse)
async def ui_bot_operations(request: Request) -> HTMLResponse:
    """Partial HTML con le ultime operazioni del bot."""

    operations = get_bot_operations(limit=50)
    regimes = get_regime_history(limit=50)
    return templates.TemplateResponse(
        "partials/bot_operations_table.html",
        {"request": request, "operations": operations, "regimes": regimes},
    )


@app.get("/ui/regime-history", response_class=HTMLResponse)
async def ui_regime_history(request: Request) -> HTMLResponse:
    """Partial HTML con le decisioni AI del regime di mercato."""

    regimes = get_regime_history(limit=50)
    return templates.TemplateResponse(
        "partials/regime_history.html",
        {"request": request, "regimes": regimes},
    )


@app.get("/ui/pnl-by-symbol", response_class=HTMLResponse)
async def ui_pnl_by_symbol(
    request: Request,
    lookback_hours: int = Query(24, ge=1, le=168),
) -> HTMLResponse:
    """Partial HTML con P&L breakdown per simbolo."""

    performances = get_pnl_by_symbol(lookback_hours=lookback_hours)
    return templates.TemplateResponse(
        "partials/pnl_by_symbol.html",
        {"request": request, "performances": performances, "lookback_hours": lookback_hours},
    )


# =====================
# HFT Metrics Proxy
# =====================

BOT_URL = os.getenv("BOT_URL", "http://app:8080")


@app.get("/hft-metrics")
async def get_hft_metrics():
    """Proxy to bot's /metrics endpoint for HFT performance data."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{BOT_URL}/metrics")
            return response.json()
    except Exception as e:
        return {"error": str(e), "bot_url": BOT_URL}


@app.get("/hft-metrics/history")
async def get_hft_metrics_history(limit: int = Query(100, ge=1, le=1000)):
    """Proxy to bot's /metrics/history endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{BOT_URL}/metrics/history", params={"limit": limit})
            return response.json()
    except Exception as e:
        return {"error": str(e)}


@app.get("/ui/hft-metrics", response_class=HTMLResponse)
async def ui_hft_metrics(request: Request) -> HTMLResponse:
    """Partial HTML con le metriche HFT."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{BOT_URL}/metrics")
            metrics = response.json()
    except Exception:
        metrics = None

    return templates.TemplateResponse(
        "partials/hft_metrics.html",
        {"request": request, "metrics": metrics},
    )


# Comodo per sviluppo locale: `python main.py`
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="localhost", port=8000, reload=True)
