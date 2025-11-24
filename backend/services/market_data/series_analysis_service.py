"""
Series Analysis Service - Fetch and format raw intraday series data for AI.

This service fetches historical candle data and calculates technical indicators (EMA, RSI, MACD)
to provide a raw time-series view for the AI, mimicking the "human eye" approach.
"""

import logging
import pandas as pd
import pandas_ta as ta
from typing import Dict, List
from services.technical_analysis_service import fetch_historical_data

logger = logging.getLogger(__name__)

def get_intraday_series_data(symbols: List[str], count: int = 12) -> Dict[str, str]:
    """
    Fetch and format raw intraday series data for the given symbols.

    Args:
        symbols: List of symbols to analyze.
        count: Number of recent data points to include (default 12).

    Returns:
        Dictionary mapping symbol to formatted XML-like string.
        Example:
        {
            "BTC": "<BTC_data>\nTime, Close, EMA_9, EMA_21, RSI, MACD, Signal\n...</BTC_data>"
        }
    """
    logger.info(f"Generating intraday series data for {len(symbols)} symbols (last {count} candles)")
    
    # Fetch historical data (70 candles to ensure enough for indicators)
    history = fetch_historical_data(symbols, period="1h", count=70)
    
    formatted_data = {}
    
    for symbol in symbols:
        if symbol not in history:
            logger.warning(f"No historical data found for {symbol}")
            continue
            
        df = history[symbol].copy()
        
        if df.empty:
            continue
            
        try:
            # Calculate Indicators
            # EMA 9 and 21 (Trend)
            df['EMA_9'] = ta.ema(df['Close'], length=9)
            df['EMA_21'] = ta.ema(df['Close'], length=21)
            
            # RSI 14 (Momentum)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # MACD (12, 26, 9) (Momentum/Trend)
            macd = ta.macd(df['Close'], fast=12, slow=26, signal=9)
            if macd is not None:
                df['MACD'] = macd['MACD_12_26_9']
                df['MACD_Signal'] = macd['MACDs_12_26_9']
            else:
                df['MACD'] = 0
                df['MACD_Signal'] = 0
                
            # Slice the last 'count' rows
            recent_df = df.tail(count)
            
            # Format as CSV-like string within XML tag
            lines = [f"<{symbol}_data>", "Time, Close, EMA_9, EMA_21, RSI, MACD, Signal"]
            
            for _, row in recent_df.iterrows():
                time_str = row['Date'].strftime('%H:%M')
                close = f"{row['Close']:.2f}"
                ema_9 = f"{row['EMA_9']:.2f}" if pd.notna(row['EMA_9']) else "NaN"
                ema_21 = f"{row['EMA_21']:.2f}" if pd.notna(row['EMA_21']) else "NaN"
                rsi = f"{row['RSI']:.1f}" if pd.notna(row['RSI']) else "NaN"
                macd_val = f"{row['MACD']:.2f}" if pd.notna(row['MACD']) else "NaN"
                macd_sig = f"{row['MACD_Signal']:.2f}" if pd.notna(row['MACD_Signal']) else "NaN"
                
                lines.append(f"{time_str}, {close}, {ema_9}, {ema_21}, {rsi}, {macd_val}, {macd_sig}")
                
            lines.append(f"</{symbol}_data>")
            
            formatted_data[symbol] = "\n".join(lines)
            
        except Exception as e:
            logger.error(f"Failed to generate series data for {symbol}: {e}")
            formatted_data[symbol] = f"<{symbol}_data>Error calculating indicators</{symbol}_data>"
            
    return formatted_data


def get_raw_indicators_for_symbols(
    symbols: List[str],
    candle_count: int = 12,
) -> Dict[str, Dict]:
    """
    Calculate raw technical indicators for symbols (Rizzo-style).

    Returns structured dict with RSI, MACD, EMA, Volume - NOT aggregated scores.
    This enables AI to reason about indicator conflicts.

    Args:
        symbols: List of symbols to analyze
        candle_count: Number of candles for intraday series (default 12)

    Returns:
        Dict mapping symbol to RawIndicators + IntradaySeries:
        {
            "BTC": {
                "raw_indicators": {
                    "rsi_7": 35.3,
                    "rsi_14": 42.1,
                    "rsi_signal": "neutral",
                    "macd": -32.25,
                    "macd_signal": -28.10,
                    ...
                },
                "intraday_series": {
                    "timestamps": [...],
                    "closes": [...],
                    ...
                }
            }
        }
    """
    logger.info(f"Calculating raw indicators for {len(symbols)} symbols (Rizzo-style)")

    # Fetch historical data (70 candles for indicator calculation)
    history = fetch_historical_data(symbols, period="1h", count=70)

    results = {}

    for symbol in symbols:
        if symbol not in history:
            logger.warning(f"No historical data for {symbol}")
            continue

        df = history[symbol].copy()
        if df.empty or len(df) < 30:
            logger.warning(f"Insufficient data for {symbol}: {len(df)} candles")
            continue

        try:
            # Calculate all indicators
            # RSI (7 and 14 periods)
            df['RSI_7'] = ta.rsi(df['Close'], length=7)
            df['RSI_14'] = ta.rsi(df['Close'], length=14)

            # MACD (12, 26, 9)
            macd_result = ta.macd(df['Close'], fast=12, slow=26, signal=9)
            if macd_result is not None:
                df['MACD'] = macd_result['MACD_12_26_9']
                df['MACD_Signal'] = macd_result['MACDs_12_26_9']
                df['MACD_Histogram'] = macd_result['MACDh_12_26_9']
            else:
                df['MACD'] = 0.0
                df['MACD_Signal'] = 0.0
                df['MACD_Histogram'] = 0.0

            # EMA (9, 21, 50)
            df['EMA_9'] = ta.ema(df['Close'], length=9)
            df['EMA_21'] = ta.ema(df['Close'], length=21)
            df['EMA_50'] = ta.ema(df['Close'], length=50)

            # Volume average (20 periods)
            df['Volume_Avg'] = df['Volume'].rolling(window=20).mean()

            # Get latest values
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest

            # Extract raw values
            rsi_7 = float(latest['RSI_7']) if pd.notna(latest['RSI_7']) else 50.0
            rsi_14 = float(latest['RSI_14']) if pd.notna(latest['RSI_14']) else 50.0
            macd = float(latest['MACD']) if pd.notna(latest['MACD']) else 0.0
            macd_signal = float(latest['MACD_Signal']) if pd.notna(latest['MACD_Signal']) else 0.0
            macd_histogram = float(latest['MACD_Histogram']) if pd.notna(latest['MACD_Histogram']) else 0.0
            ema_9 = float(latest['EMA_9']) if pd.notna(latest['EMA_9']) else float(latest['Close'])
            ema_21 = float(latest['EMA_21']) if pd.notna(latest['EMA_21']) else float(latest['Close'])
            ema_50 = float(latest['EMA_50']) if pd.notna(latest['EMA_50']) else float(latest['Close'])
            volume_current = float(latest['Volume'])
            volume_avg = float(latest['Volume_Avg']) if pd.notna(latest['Volume_Avg']) else volume_current
            current_price = float(latest['Close'])

            # Calculate signals
            rsi_signal = "oversold" if rsi_14 < 30 else "overbought" if rsi_14 > 70 else "neutral"

            # MACD crossover detection
            prev_macd = float(prev['MACD']) if pd.notna(prev['MACD']) else 0.0
            prev_signal = float(prev['MACD_Signal']) if pd.notna(prev['MACD_Signal']) else 0.0
            if prev_macd <= prev_signal and macd > macd_signal:
                macd_crossover = "bullish"
            elif prev_macd >= prev_signal and macd < macd_signal:
                macd_crossover = "bearish"
            else:
                macd_crossover = "none"

            # EMA trend (alignment)
            if ema_9 > ema_21 > ema_50 and current_price > ema_9:
                ema_trend = "strong_up"
            elif ema_9 > ema_21 and current_price > ema_21:
                ema_trend = "up"
            elif ema_9 < ema_21 < ema_50 and current_price < ema_9:
                ema_trend = "strong_down"
            elif ema_9 < ema_21 and current_price < ema_21:
                ema_trend = "down"
            else:
                ema_trend = "neutral"

            # Volume signal
            volume_ratio = volume_current / volume_avg if volume_avg > 0 else 1.0
            volume_signal = "high" if volume_ratio > 1.2 else "low" if volume_ratio < 0.8 else "normal"

            # Price vs EMA percentages
            price_vs_ema9_pct = ((current_price - ema_9) / ema_9 * 100) if ema_9 > 0 else 0.0
            price_vs_ema21_pct = ((current_price - ema_21) / ema_21 * 100) if ema_21 > 0 else 0.0

            # Build raw indicators dict
            raw_indicators = {
                "rsi_7": round(rsi_7, 2),
                "rsi_14": round(rsi_14, 2),
                "rsi_signal": rsi_signal,
                "macd": round(macd, 4),
                "macd_signal": round(macd_signal, 4),
                "macd_histogram": round(macd_histogram, 4),
                "macd_crossover": macd_crossover,
                "ema_9": round(ema_9, 4),
                "ema_21": round(ema_21, 4),
                "ema_50": round(ema_50, 4),
                "ema_trend": ema_trend,
                "volume_current": round(volume_current, 2),
                "volume_avg": round(volume_avg, 2),
                "volume_ratio": round(volume_ratio, 2),
                "volume_signal": volume_signal,
                "price_vs_ema9_pct": round(price_vs_ema9_pct, 2),
                "price_vs_ema21_pct": round(price_vs_ema21_pct, 2),
            }

            # Build intraday series (last N candles)
            recent_df = df.tail(candle_count)
            intraday_series = {
                "timestamps": [row['Date'].isoformat() for _, row in recent_df.iterrows()],
                "opens": [round(float(row['Open']), 4) for _, row in recent_df.iterrows()],
                "highs": [round(float(row['High']), 4) for _, row in recent_df.iterrows()],
                "lows": [round(float(row['Low']), 4) for _, row in recent_df.iterrows()],
                "closes": [round(float(row['Close']), 4) for _, row in recent_df.iterrows()],
                "volumes": [round(float(row['Volume']), 2) for _, row in recent_df.iterrows()],
                "candle_count": len(recent_df),
                "interval": "1h",
            }

            results[symbol] = {
                "raw_indicators": raw_indicators,
                "intraday_series": intraday_series,
            }

        except Exception as e:
            logger.error(f"Failed to calculate raw indicators for {symbol}: {e}", exc_info=True)
            continue

    logger.info(f"Calculated raw indicators for {len(results)}/{len(symbols)} symbols")
    return results
