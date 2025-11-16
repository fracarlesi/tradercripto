from __future__ import annotations
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import traceback
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

# Import opzionale di numpy per gestire tipi np.float64 / np.int64, ecc.
try:  # pragma: no cover - se numpy non è installato non è un problema
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None  # type: ignore

load_dotenv()



@dataclass
class DBConfig:
    dsn: str


def get_db_config() -> DBConfig:
    """Recupera la configurazione del DB dalla variabile d'ambiente DATABASE_URL.

    Esempio:
    export DATABASE_URL="postgresql://user:password@localhost:5432/trading_db"
    """

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL non impostata. Imposta la variabile d'ambiente, "
            "ad esempio: postgresql://user:password@localhost:5432/trading_db"
        )
    return DBConfig(dsn=dsn)


@contextmanager
def get_connection():
    """Context manager che restituisce una connessione PostgreSQL.

    Usa il DSN in DATABASE_URL.
    """

    config = get_db_config()
    conn = psycopg2.connect(config.dsn)
    try:
        yield conn
    finally:
        conn.close()


# =====================
# Creazione schema
# =====================


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    balance_usd     NUMERIC(20, 8) NOT NULL,
    raw_payload     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS open_positions (
    id                  BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES account_snapshots(id) ON DELETE CASCADE,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    size                NUMERIC(30, 10) NOT NULL,
    entry_price         NUMERIC(30, 10),
    mark_price          NUMERIC(30, 10),
    pnl_usd             NUMERIC(30, 10),
    leverage            TEXT,
    raw_payload         JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_open_positions_snapshot_id
    ON open_positions(snapshot_id);


CREATE TABLE IF NOT EXISTS ai_contexts (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    system_prompt   TEXT
);

CREATE TABLE IF NOT EXISTS indicators_contexts (
    id                      BIGSERIAL PRIMARY KEY,
    context_id              BIGINT NOT NULL REFERENCES ai_contexts(id) ON DELETE CASCADE,
    ticker                  TEXT NOT NULL,
    ts                      TIMESTAMPTZ,
    price                   NUMERIC(20, 8),
    ema20                   NUMERIC(20, 8),
    macd                    NUMERIC(20, 8),
    rsi_7                   NUMERIC(20, 8),
    volume_bid              NUMERIC(20, 8),
    volume_ask              NUMERIC(20, 8),
    pp                      NUMERIC(20, 8),
    s1                      NUMERIC(20, 8),
    s2                      NUMERIC(20, 8),
    r1                      NUMERIC(20, 8),
    r2                      NUMERIC(20, 8),
    open_interest_latest    NUMERIC(30, 10),
    open_interest_average   NUMERIC(30, 10),
    funding_rate            NUMERIC(20, 8),
    ema20_15m               NUMERIC(20, 8),
    ema50_15m               NUMERIC(20, 8),
    atr3_15m                NUMERIC(20, 8),
    atr14_15m               NUMERIC(20, 8),
    volume_15m_current      NUMERIC(30, 10),
    volume_15m_average      NUMERIC(30, 10),
    intraday_mid_prices     JSONB,
    intraday_ema20_series   JSONB,
    intraday_macd_series    JSONB,
    intraday_rsi7_series    JSONB,
    intraday_rsi14_series   JSONB,
    lt15m_macd_series       JSONB,
    lt15m_rsi14_series      JSONB
);

CREATE TABLE IF NOT EXISTS news_contexts (
    id              BIGSERIAL PRIMARY KEY,
    context_id      BIGINT NOT NULL REFERENCES ai_contexts(id) ON DELETE CASCADE,
    news_text       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sentiment_contexts (
    id                      BIGSERIAL PRIMARY KEY,
    context_id              BIGINT NOT NULL REFERENCES ai_contexts(id) ON DELETE CASCADE,
    value                   INTEGER,
    classification          TEXT,
    sentiment_timestamp     BIGINT,
    raw                     JSONB
);

CREATE TABLE IF NOT EXISTS forecasts_contexts (
    id                      BIGSERIAL PRIMARY KEY,
    context_id              BIGINT NOT NULL REFERENCES ai_contexts(id) ON DELETE CASCADE,
    ticker                  TEXT NOT NULL,
    timeframe               TEXT NOT NULL,
    last_price              NUMERIC(30, 10),
    prediction              NUMERIC(30, 10),
    lower_bound             NUMERIC(30, 10),
    upper_bound             NUMERIC(30, 10),
    change_pct              NUMERIC(10, 4),
    forecast_timestamp      BIGINT,
    raw                     JSONB
);

CREATE TABLE IF NOT EXISTS bot_operations (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    context_id          BIGINT REFERENCES ai_contexts(id) ON DELETE CASCADE,
    operation           TEXT NOT NULL,
    symbol              TEXT,
    direction           TEXT,
    target_portion_of_balance NUMERIC(10, 4),
    leverage            NUMERIC(10, 4),
    raw_payload         JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_operations_created_at
    ON bot_operations(created_at);

CREATE TABLE IF NOT EXISTS errors (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_type      TEXT NOT NULL,
    error_message   TEXT,
    traceback       TEXT,
    context         JSONB,
    source          TEXT
);

CREATE INDEX IF NOT EXISTS idx_errors_created_at
    ON errors(created_at);
"""


MIGRATION_SQL = """
ALTER TABLE bot_operations
    ADD COLUMN IF NOT EXISTS context_id BIGINT;

ALTER TABLE indicators_contexts
    ADD COLUMN IF NOT EXISTS ticker TEXT,
    ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS price NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS ema20 NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS macd NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS rsi_7 NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS volume_bid NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS volume_ask NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS pp NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS s1 NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS s2 NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS r1 NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS r2 NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS open_interest_latest NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS open_interest_average NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS funding_rate NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS ema20_15m NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS ema50_15m NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS atr3_15m NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS atr14_15m NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS volume_15m_current NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS volume_15m_average NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS intraday_mid_prices JSONB,
    ADD COLUMN IF NOT EXISTS intraday_ema20_series JSONB,
    ADD COLUMN IF NOT EXISTS intraday_macd_series JSONB,
    ADD COLUMN IF NOT EXISTS intraday_rsi7_series JSONB,
    ADD COLUMN IF NOT EXISTS intraday_rsi14_series JSONB,
    ADD COLUMN IF NOT EXISTS lt15m_macd_series JSONB,
    ADD COLUMN IF NOT EXISTS lt15m_rsi14_series JSONB;

ALTER TABLE sentiment_contexts
    ADD COLUMN IF NOT EXISTS value INTEGER,
    ADD COLUMN IF NOT EXISTS classification TEXT,
    ADD COLUMN IF NOT EXISTS sentiment_timestamp BIGINT,
    ADD COLUMN IF NOT EXISTS raw JSONB;

ALTER TABLE forecasts_contexts
    ADD COLUMN IF NOT EXISTS ticker TEXT,
    ADD COLUMN IF NOT EXISTS timeframe TEXT,
    ADD COLUMN IF NOT EXISTS last_price NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS prediction NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS lower_bound NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS upper_bound NUMERIC(30, 10),
    ADD COLUMN IF NOT EXISTS change_pct NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS forecast_timestamp BIGINT,
    ADD COLUMN IF NOT EXISTS raw JSONB;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'indicators_contexts'
          AND column_name = 'indicators'
    ) THEN
        ALTER TABLE indicators_contexts
        ALTER COLUMN indicators DROP NOT NULL;
    END IF;
END$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'sentiment_contexts'
          AND column_name = 'sentiment'
    ) THEN
        ALTER TABLE sentiment_contexts
        ALTER COLUMN sentiment DROP NOT NULL;
    END IF;
END$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'forecasts_contexts'
          AND column_name = 'forecasts'
    ) THEN
        ALTER TABLE forecasts_contexts
        ALTER COLUMN forecasts DROP NOT NULL;
    END IF;
END$$;
"""



def init_db() -> None:
    """Crea le tabelle necessarie nel database se non esistono.

    Da chiamare una volta all'avvio dell'applicazione.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Crea le tabelle base
            cur.execute(SCHEMA_SQL)
            # Applica eventuali migrazioni (aggiunta colonne per input del modello)
            cur.execute(MIGRATION_SQL)
        conn.commit()


# =====================
# Funzioni di logging
# =====================


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_json_arg(value: Any) -> Any:
    """Normalizza un argomento che può essere dict/list oppure stringa JSON.

    - Se è una stringa, prova a fare json.loads; in caso di errore, la incapsula in {"raw": value}
    - Altrimenti la restituisce così com'è.
    """

    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {"raw": value}
    return value


def _to_plain_number(value: Any) -> Optional[float]:
    """Converte numeri (inclusi numpy scalars) in float Python.

    Restituisce None se non convertibile.
    """

    if value is None:
        return None

    # Gestione numpy scalars se numpy è disponibile
    if np is not None:  # type: ignore[name-defined]
        try:
            if isinstance(value, np.generic):  # type: ignore[attr-defined]
                return float(value)  # type: ignore[arg-type]
        except Exception:
            pass

    if isinstance(value, (int, float)):
        return float(value)

    try:
        return float(value)
    except Exception:
        return None


def _normalize_for_json(value: Any) -> Any:
    """Converte strutture (dict/list) sostituendo eventuali numpy scalars con tipi Python.

    Utile prima di passare a Json(...).
    """

    if isinstance(value, dict):
        return {k: _normalize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_for_json(v) for v in value]

    num = _to_plain_number(value)
    if num is not None:
        return num

    return value


def log_error(
    exc: BaseException,
    *,
    context: Optional[Dict[str, Any]] = None,
    source: Optional[str] = None,
) -> None:
    """Salva un'eccezione nella tabella `errors`.

    Parametri:
    - exc: eccezione catturata (es. nell'`except Exception as e:`)
    - context: dizionario opzionale con informazioni aggiuntive (verrà salvato come JSONB)
    - source: stringa opzionale per indicare la sorgente (es. "main_loop", "news_feed", ...)

    Uso tipico::

        try:
            ...
        except Exception as e:
            log_error(e, context={"phase": "main_loop"}, source="trading_agent")
            raise
    """

    error_type = type(exc).__name__
    error_message = str(exc)
    tb_str = traceback.format_exc()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO errors (
                    error_type,
                    error_message,
                    traceback,
                    context,
                    source
                )
                VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    error_type,
                    error_message,
                    tb_str,
                    Json(context) if context is not None else None,
                    source,
                ),
            )
        conn.commit()



def log_account_status(account_status: Dict[str, Any]) -> int:
    """Logga lo stato dell'account e le posizioni aperte.

    `account_status` è atteso in un formato del tipo:
    {
        "balance_usd": 996.818505,
        "open_positions": [
            {
                "symbol": "BNB",
                "side": "long",
                "size": 0.106,
                "entry_price": 932.54,
                "mark_price": 932.745,
                "pnl_usd": 0.0217,
                "leverage": "2x (cross)",
            },
            ...
        ],
    }

    Restituisce l'ID dello snapshot creato.
    """

    balance = account_status.get("balance_usd")
    if balance is None:
        raise ValueError("account_status deve contenere 'balance_usd'")

    open_positions_data = account_status.get("open_positions") or []

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Inserisci lo snapshot dell'account
            cur.execute(
                """
                INSERT INTO account_snapshots (balance_usd, raw_payload)
                VALUES (%s, %s)
                RETURNING id;
                """,
                (balance, Json(account_status)),
            )
            snapshot_id = cur.fetchone()[0]

            # Inserisci una riga per ciascuna posizione aperta
            for pos in open_positions_data:
                symbol = pos.get("symbol")
                side = pos.get("side")
                size = pos.get("size")
                entry_price = pos.get("entry_price")
                mark_price = pos.get("mark_price")
                pnl_usd = pos.get("pnl_usd")
                leverage = pos.get("leverage")

                cur.execute(
                    """
                    INSERT INTO open_positions (
                        snapshot_id,
                        symbol,
                        side,
                        size,
                        entry_price,
                        mark_price,
                        pnl_usd,
                        leverage,
                        raw_payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        snapshot_id,
                        symbol,
                        side,
                        size,
                        entry_price,
                        mark_price,
                        pnl_usd,
                        leverage,
                        Json(pos),
                    ),
                )

        conn.commit()

    return snapshot_id


def log_bot_operation(
    operation_payload: Dict[str, Any],
    *,
    system_prompt: Optional[str] = None,
    indicators: Optional[Any] = None,
    news_text: Optional[str] = None,
    sentiment: Optional[Any] = None,
    forecasts: Optional[Any] = None,
) -> int:
    """Logga un'operazione del bot e tutti gli input associati.

    Modello dati:
    - Crea un record in `ai_contexts` (sempre, anche se alcuni campi sono None)
    - Se presenti, crea record nelle tabelle:
        - `indicators_contexts` (indicators)
        - `news_contexts` (news_text)
        - `sentiment_contexts` (sentiment)
        - `forecasts_contexts` (forecasts)
    - Crea una riga in `bot_operations` collegata via `context_id`.

    Parametri:
    - operation_payload: dict con i campi principali dell'operazione, es:
        {
            "operation": "open",
            "symbol": "BTC",
            "direction": "long",
            "target_portion_of_balance": 0.3,
            "leverage": 3,
            "reason": "...",
            ...
        }
    - system_prompt: stringa con il prompt di sistema completo usato dall'agente
    - indicators: dict/list (o stringa JSON) con gli indici per ticker
    - news_text: testo con le news rilevanti
    - sentiment: dict (o stringa JSON), es: {"valore": 16, "classificazione": "Extreme fear", ...}
    - forecasts: lista/dict (o stringa JSON) con i forecast per ticker/timeframe

    Restituisce l'ID dell'operazione creata.
    """

    operation = operation_payload.get("operation")
    if operation is None:
        raise ValueError("operation_payload deve contenere 'operation'")

    symbol = operation_payload.get("symbol")
    direction = operation_payload.get("direction")
    target_portion_of_balance = operation_payload.get("target_portion_of_balance")
    leverage = operation_payload.get("leverage")

    # indicators_norm = _normalize_json_arg(indicators) if indicators is not None else None
    sentiment_norm = _normalize_json_arg(sentiment) if sentiment is not None else None
    forecasts_norm = _normalize_json_arg(forecasts) if forecasts is not None else None

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1) Crea il contesto generale
            cur.execute(
                """
                INSERT INTO ai_contexts (system_prompt)
                VALUES (%s)
                RETURNING id;
                """,
                (system_prompt,),
            )
            context_id = cur.fetchone()[0]
            if indicators is not None:
                for indicator in indicators:
                    indicators_norm = _normalize_json_arg(indicator) if indicator is not None else None

                    # 2) Dettagli per tipo di input, se presenti
                    if indicators_norm is not None:
                        # indicators_norm può essere:
                        # - un dict con chiave "ticker" (un solo ticker)
                        # - un dict {ticker: {...}}
                        # - una lista di dict
                        indicator_items: List[Dict[str, Any]] = []

                        if isinstance(indicators_norm, dict):
                            if "ticker" in indicators_norm:
                                indicator_items = [indicators_norm]
                            else:
                                for tkr, data in indicators_norm.items():
                                    if isinstance(data, dict):
                                        item = {"ticker": tkr}
                                        item.update(data)
                                        indicator_items.append(item)
                        elif isinstance(indicators_norm, list):
                            indicator_items = [x for x in indicators_norm if isinstance(x, dict)]

                        for item in indicator_items:
                            ticker = item.get("ticker")
                            if not ticker:
                                continue

                            ts = None
                            ts_raw = item.get("timestamp")
                            if isinstance(ts_raw, str):
                                try:
                                    ts = datetime.fromisoformat(ts_raw)
                                except Exception:
                                    ts = None

                            current = item.get("current") or {}
                            pivot = item.get("pivot_points") or {}
                            derivatives = item.get("derivatives") or {}
                            intraday = item.get("intraday") or {}
                            lt15 = item.get("longer_term_15m") or {}

                            # Volume: "Bid Vol: 1018.14, Ask Vol: 350.96"
                            volume_str = item.get("volume") or ""
                            volume_bid = None
                            volume_ask = None
                            if isinstance(volume_str, str) and "Bid Vol" in volume_str:
                                try:
                                    parts = volume_str.replace("Bid Vol:", "").split("Ask Vol:")
                                    bid_str = parts[0].strip().strip(",")
                                    ask_str = parts[1].strip()
                                    volume_bid = float(bid_str)
                                    volume_ask = float(ask_str)
                                except Exception:
                                    volume_bid = None
                                    volume_ask = None

                            # CORREZIONE: Query con 30 placeholder correttamente formattati
                            cur.execute(
                                """
                                INSERT INTO indicators_contexts (
                                    context_id,
                                    ticker,
                                    ts,
                                    price,
                                    ema20,
                                    macd,
                                    rsi_7,
                                    volume_bid,
                                    volume_ask,
                                    pp,
                                    s1,
                                    s2,
                                    r1,
                                    r2,
                                    open_interest_latest,
                                    open_interest_average,
                                    funding_rate,
                                    ema20_15m,
                                    ema50_15m,
                                    atr3_15m,
                                    atr14_15m,
                                    volume_15m_current,
                                    volume_15m_average,
                                    intraday_mid_prices,
                                    intraday_ema20_series,
                                    intraday_macd_series,
                                    intraday_rsi7_series,
                                    intraday_rsi14_series,
                                    lt15m_macd_series,
                                    lt15m_rsi14_series
                                )
                                VALUES (
                                    %s, %s, %s,
                                    %s, %s, %s, %s,
                                    %s, %s,
                                    %s, %s, %s, %s, %s,
                                    %s, %s, %s,
                                    %s, %s, %s, %s,
                                    %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s
                                );
                                """,
                                (
                                    context_id,
                                    ticker,
                                    ts,
                                    _to_plain_number(current.get("price")),
                                    _to_plain_number(current.get("ema20")),
                                    _to_plain_number(current.get("macd")),
                                    _to_plain_number(current.get("rsi_7")),
                                    _to_plain_number(volume_bid),
                                    _to_plain_number(volume_ask),
                                    _to_plain_number(pivot.get("pp")),
                                    _to_plain_number(pivot.get("s1")),
                                    _to_plain_number(pivot.get("s2")),
                                    _to_plain_number(pivot.get("r1")),
                                    _to_plain_number(pivot.get("r2")),
                                    _to_plain_number(derivatives.get("open_interest_latest")),
                                    _to_plain_number(derivatives.get("open_interest_average")),
                                    _to_plain_number(derivatives.get("funding_rate")),
                                    _to_plain_number(lt15.get("ema_20_current")),
                                    _to_plain_number(lt15.get("ema_50_current")),
                                    _to_plain_number(lt15.get("atr_3_current")),
                                    _to_plain_number(lt15.get("atr_14_current")),
                                    _to_plain_number(lt15.get("volume_current")),
                                    _to_plain_number(lt15.get("volume_average")),
                                    Json(_normalize_for_json(intraday.get("mid_prices"))) if intraday.get("mid_prices") is not None else None,
                                    Json(_normalize_for_json(intraday.get("ema_20"))) if intraday.get("ema_20") is not None else None,
                                    Json(_normalize_for_json(intraday.get("macd"))) if intraday.get("macd") is not None else None,
                                    Json(_normalize_for_json(intraday.get("rsi_7"))) if intraday.get("rsi_7") is not None else None,
                                    Json(_normalize_for_json(intraday.get("rsi_14"))) if intraday.get("rsi_14") is not None else None,
                                    Json(_normalize_for_json(lt15.get("macd_series"))) if lt15.get("macd_series") is not None else None,
                                    Json(_normalize_for_json(lt15.get("rsi_14_series"))) if lt15.get("rsi_14_series") is not None else None,
                                ),
                            )



            if news_text:
                cur.execute(
                    """
                    INSERT INTO news_contexts (context_id, news_text)
                    VALUES (%s, %s);
                    """,
                    (context_id, news_text),
                )

            if sentiment_norm is not None:
                value = sentiment_norm.get("valore")
                classification = sentiment_norm.get("classificazione")
                ts_raw = sentiment_norm.get("timestamp")
                try:
                    ts_val = int(ts_raw) if ts_raw is not None else None
                except Exception:
                    ts_val = None

                cur.execute(
                    """
                    INSERT INTO sentiment_contexts (context_id, value, classification, sentiment_timestamp, raw)
                    VALUES (%s, %s, %s, %s, %s);
                    """,
                    (context_id, value, classification, ts_val, Json(sentiment_norm)),
                )


            if forecasts_norm is not None:
                forecast_items: List[Dict[str, Any]] = []
                if isinstance(forecasts_norm, list):
                    forecast_items = [x for x in forecasts_norm if isinstance(x, dict)]
                elif isinstance(forecasts_norm, dict):
                    forecast_items = [forecasts_norm]

                for f in forecast_items:
                    ticker = f.get("Ticker") or f.get("ticker")
                    timeframe = f.get("Timeframe") or f.get("timeframe")
                    last_price = f.get("Ultimo Prezzo") or f.get("last_price")
                    prediction = f.get("Previsione") or f.get("prediction")
                    lower = f.get("Limite Inferiore") or f.get("lower_bound")
                    upper = f.get("Limite Superiore") or f.get("upper_bound")
                    change_pct = f.get("Variazione %") or f.get("change_pct")
                    ts_raw = f.get("Timestamp Previsione") or f.get("forecast_timestamp")
                    try:
                        ts_val = int(ts_raw) if ts_raw is not None else None
                    except Exception:
                        ts_val = None

                    if not ticker or not timeframe:
                        continue

                    cur.execute(
                        """
                        INSERT INTO forecasts_contexts (
                            context_id,
                            ticker,
                            timeframe,
                            last_price,
                            prediction,
                            lower_bound,
                            upper_bound,
                            change_pct,
                            forecast_timestamp,
                            raw
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """,
                        (
                            context_id,
                            ticker,
                            timeframe,
                            _to_plain_number(last_price),
                            _to_plain_number(prediction),
                            _to_plain_number(lower),
                            _to_plain_number(upper),
                            _to_plain_number(change_pct),
                            ts_val,
                            Json(_normalize_for_json(f)),
                        ),
                    )


            # 3) Operazione del bot collegata al contesto
            cur.execute(
                """
                INSERT INTO bot_operations (
                    context_id,
                    operation,
                    symbol,
                    direction,
                    target_portion_of_balance,
                    leverage,
                    raw_payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    context_id,
                    operation,
                    symbol,
                    direction,
                    target_portion_of_balance,
                    leverage,
                    Json(operation_payload),
                ),
            )
            op_id = cur.fetchone()[0]

        conn.commit()

    return op_id




# =====================
# Funzioni di lettura (facoltative ma utili)
# =====================


def get_latest_account_snapshot() -> Optional[Dict[str, Any]]:
    """Restituisce l'ultimo snapshot dell'account (raw_payload) oppure None."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT raw_payload
                FROM account_snapshots
                ORDER BY created_at DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            return row[0]



def get_recent_bot_operations(limit: int = 50) -> List[Dict[str, Any]]:
    """Restituisce le ultime N operazioni del bot (raw_payload)."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT raw_payload
                FROM bot_operations
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [r[0] for r in rows]


if __name__ == "__main__":
    init_db()

    # snapshot_id = log_account_status(example_account_status)
    # print(f"[db_utils] Operazione inserita con id={snapshot_id}")
    # operation
    # op_id = log_bot_operation(
    #     example_operation,
    #     system_prompt=example_system_prompt,
    #     indicators=example_indicators,
    #     news_text=example_news_text,
    #     sentiment=example_sentiment,
    #     forecasts=example_forecasts,
    # )
    # print(f"[db_utils] Operazione inserita con id={op_id}")
