import pandas as pd
from datetime import datetime, timezone, timedelta
from prophet import Prophet
from hyperliquid.info import Info
from hyperliquid.utils import constants
import warnings
warnings.filterwarnings('ignore')

class HyperliquidForecaster:
    def __init__(self, testnet: bool = True):
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True)
        self.last_prices = {}  # Memorizza gli ultimi prezzi per calcolare la variazione

    def _fetch_candles(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        interval_ms = {"15m": 15*60_000, "1h": 60*60_000}[interval]
        start_ms = now_ms - limit * interval_ms

        data = self.info.candles_snapshot(
            name=coin,
            interval=interval,
            startTime=start_ms,
            endTime=now_ms
        )

        if not data:
            raise RuntimeError(f"No candles for {coin} {interval}")

        df = pd.DataFrame(data)
        df["ds"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(None)
        df["y"] = df["c"].astype(float)

        df = df[["ds", "y"]].sort_values("ds").reset_index(drop=True)
        return df

    def forecast(self, coin: str, interval: str) -> tuple:
        if interval == "15m":
            df = self._fetch_candles(coin, "15m", limit=300)
            freq = "15min"
        else:
            df = self._fetch_candles(coin, "1h", limit=500)
            freq = "H"

        # Memorizza l'ultimo prezzo
        last_price = df["y"].iloc[-1]

        model = Prophet(daily_seasonality=True, weekly_seasonality=True)
        model.fit(df)

        future = model.make_future_dataframe(periods=1, freq=freq)
        forecast = model.predict(future)

        # Restituisce sia il forecast che l'ultimo prezzo
        return forecast.tail(1)[["ds", "yhat", "yhat_lower", "yhat_upper"]], last_price

    def forecast_many(self, tickers: list, intervals=("15m", "1h")):
        results = []
        for coin in tickers:
            for interval in intervals:
                try:
                    forecast_data, last_price = self.forecast(coin, interval)
                    fc = forecast_data.iloc[0]
                    
                    # Calcola la variazione percentuale
                    variazione_pct = ((fc["yhat"] - last_price) / last_price) * 100
                    
                    # Determina il timeframe in italiano
                    timeframe = "Prossimi 15 Minuti" if interval == "15m" else "Prossima Ora"
                    
                    results.append({
                        "Ticker": coin,
                        "Timeframe": timeframe,
                        "Ultimo Prezzo": round(last_price, 2),
                        "Previsione": round(fc["yhat"], 2),
                        "Limite Inferiore": round(fc["yhat_lower"], 2),
                        "Limite Superiore": round(fc["yhat_upper"], 2),
                        "Variazione %": round(variazione_pct, 2),
                        "Timestamp Previsione": fc["ds"]
                    })
                except Exception as e:
                    results.append({
                        "Ticker": coin,
                        "Timeframe": "Prossimi 15 Minuti" if interval == "15m" else "Prossima Ora",
                        "Ultimo Prezzo": None,
                        "Previsione": None,
                        "Limite Inferiore": None,
                        "Limite Superiore": None,
                        "Variazione %": None,
                        "Timestamp Previsione": None,
                        "error": str(e)
                    })
        return results

    def get_predictions_summary(self) -> pd.DataFrame:
        """Restituisce un DataFrame con il riepilogo delle previsioni (compatibile con il vecchio script)"""
        if not hasattr(self, '_last_results'):
            return pd.DataFrame()
        return pd.DataFrame(self._last_results)

    def get_crypto_forecasts(self, tickers: list):
        """Metodo principale compatibile con il vecchio script"""
        self._last_results = self.forecast_many(tickers, intervals=("15m", "1h"))
        df = pd.DataFrame(self._last_results)
        
        # Rimuovi la colonna error se presente
        if 'error' in df.columns:
            df = df.drop('error', axis=1)
            
        return df.to_string(index=False)

# Funzione helper per mantenere compatibilità con il vecchio script
def get_hyperliquid_forecasts(tickers=['BTC', 'ETH', 'SOL'], testnet=True):
    forecaster = HyperliquidForecaster(testnet=testnet)
    return forecaster.get_crypto_forecasts(tickers)

def get_crypto_forecasts(tickers=['BTC', 'ETH', 'SOL'], testnet=True):
    try:
        forecaster = HyperliquidForecaster(testnet=True)
        results = forecaster.forecast_many(["BTC", "ETH", "SOL"])
        
        # Stampa il riepilogo come DataFrame
        df = pd.DataFrame(results)
        return df.to_string(index=False), df.to_json(orient='records')
    except:
        return None, None


# Esempio di utilizzo
# if __name__ == "__main__":
#     print(get_crypto_forecasts())