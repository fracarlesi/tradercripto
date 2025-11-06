import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
from prophet import Prophet
import plotly.graph_objs as go
from typing import Dict, Tuple, List
import warnings
warnings.filterwarnings('ignore')

class CryptoForecaster:
    """
    Sistema generalizzato per il forecasting di criptovalute con Prophet
    Supporta previsioni a 1 minuto e 1 ora
    """
    
    def __init__(self, tickers: List[str]):
        """
        Inizializza il forecaster con una lista di ticker
        
        Args:
            tickers: Lista di ticker da analizzare (es. ['BTC-USD', 'ETH-USD'])
        """
        self.tickers = tickers
        self.data = {}
        self.forecasts = {}
        
    def download_data(self, timeframe: str = 'both'):
        """
        Scarica i dati per tutti i ticker con la granularità appropriata
        
        Args:
            timeframe: 'minute', 'hour', o 'both'
        """
        for ticker in self.tickers:
            print(f"Scaricando dati per {ticker}...")
            
            if timeframe in ['minute', 'both']:
                # Per previsioni al minuto, scarica dati degli ultimi 7 giorni con intervallo 1m
                self.data[f"{ticker}_1m"] = self._download_minute_data(ticker)
                
            if timeframe in ['hour', 'both']:
                # Per previsioni all'ora, scarica dati degli ultimi 90 giorni con intervallo 1h
                self.data[f"{ticker}_1h"] = self._download_hourly_data(ticker)
    
    def _download_minute_data(self, ticker: str) -> pd.DataFrame:
        """Scarica dati con granularità al minuto (ultimi 7 giorni)"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)
        
        try:
            df = yf.download(
                ticker, 
                start=start_date,
                end=end_date,
                interval='1m',
                progress=False
            )
            
            if df.empty:
                print(f"Nessun dato al minuto disponibile per {ticker}")
                return pd.DataFrame()
                
            df = df.reset_index()
            df = df[['Datetime', 'Close']]
            df.columns = ['ds', 'y']
            df['ds'] = pd.to_datetime(df['ds'])
            
            # Rimuovi timezone per Prophet
            df['ds'] = df['ds'].dt.tz_localize(None)
            
            print(f"Scaricati {len(df)} record al minuto per {ticker}")
            return df
            
        except Exception as e:
            print(f"Errore nel download dati al minuto per {ticker}: {e}")
            return pd.DataFrame()
    
    def _download_hourly_data(self, ticker: str) -> pd.DataFrame:
        """Scarica dati con granularità oraria (ultimi 90 giorni)"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=90)
        
        try:
            df = yf.download(
                ticker, 
                start=start_date,
                end=end_date,
                interval='1h',
                progress=False
            )
            
            if df.empty:
                print(f"Nessun dato orario disponibile per {ticker}")
                return pd.DataFrame()
                
            df = df.reset_index()
            df = df[['Datetime', 'Close']]
            df.columns = ['ds', 'y']
            df['ds'] = pd.to_datetime(df['ds'])
            
            # Rimuovi timezone per Prophet
            df['ds'] = df['ds'].dt.tz_localize(None)
            
            print(f"Scaricati {len(df)} record orari per {ticker}")
            return df
            
        except Exception as e:
            print(f"Errore nel download dati orari per {ticker}: {e}")
            return pd.DataFrame()
    
    def forecast_all(self):
        """Genera previsioni per tutti i ticker e timeframe disponibili"""
        for key, data in self.data.items():
            if data.empty:
                continue
                
            ticker, interval = key.rsplit('_', 1)
            
            if interval == '1m':
                # Prevedi il prossimo minuto (1 periodo)
                periods = 1
                forecast_name = f"{ticker}_next_minute"
            else:  # 1h
                # Prevedi la prossima ora (1 periodo)
                periods = 1
                forecast_name = f"{ticker}_next_hour"
            
            print(f"Generando forecast per {forecast_name}...")
            forecast_df, model = self._generate_forecast(data, periods)
            
            self.forecasts[forecast_name] = {
                'forecast': forecast_df,
                'model': model,
                'data': data,
                'ticker': ticker,
                'interval': interval
            }
    
    def _generate_forecast(self, data: pd.DataFrame, periods: int) -> Tuple[pd.DataFrame, Prophet]:
        """Genera forecast usando Prophet"""
        # Configura Prophet con parametri ottimizzati per crypto
        model = Prophet(
            daily_seasonality=True,
            weekly_seasonality=True,
            yearly_seasonality=False,
            changepoint_prior_scale=0.05,
            interval_width=0.95
        )
        
        # Fit del modello
        model.fit(data)
        
        # Genera previsioni
        future = model.make_future_dataframe(periods=periods, freq='T' if periods == 1 else 'H')
        forecast = model.predict(future)
        
        return forecast, model
    
    def get_predictions_summary(self) -> pd.DataFrame:
        """Restituisce un riepilogo di tutte le previsioni"""
        summary_data = []
        
        for name, forecast_data in self.forecasts.items():
            forecast = forecast_data['forecast']
            data = forecast_data['data']
            
            # Prendi l'ultima previsione (quella futura)
            last_forecast = forecast.iloc[-1]
            
            # Prendi l'ultimo valore reale
            last_actual = data['y'].iloc[-1]
            
            summary_data.append({
                'Ticker': forecast_data['ticker'],
                'Timeframe': 'Prossimo Minuto' if forecast_data['interval'] == '1m' else 'Prossima Ora',
                'Ultimo Prezzo': round(last_actual, 2),
                'Previsione': round(last_forecast['yhat'], 2),
                'Limite Inferiore': round(last_forecast['yhat_lower'], 2),
                'Limite Superiore': round(last_forecast['yhat_upper'], 2),
                'Variazione %': round(((last_forecast['yhat'] - last_actual) / last_actual) * 100, 2),
                'Timestamp Previsione': last_forecast['ds']
            })
        
        return pd.DataFrame(summary_data)
    
    def plot_forecast(self, ticker: str, timeframe: str = 'both'):
        """Visualizza i grafici delle previsioni per un ticker specifico"""
        
        if timeframe in ['minute', 'both']:
            self._plot_single_forecast(f"{ticker}_next_minute")
            
        if timeframe in ['hour', 'both']:
            self._plot_single_forecast(f"{ticker}_next_hour")
    
    def _plot_single_forecast(self, forecast_name: str):
        """Crea un grafico interattivo per una singola previsione"""
        if forecast_name not in self.forecasts:
            print(f"Nessuna previsione trovata per {forecast_name}")
            return
            
        forecast_data = self.forecasts[forecast_name]
        df = forecast_data['data']
        forecast = forecast_data['forecast']
        ticker = forecast_data['ticker']
        interval = forecast_data['interval']
        
        # Crea il grafico
        fig = go.Figure()
        
        # Aggiungi prezzi reali
        fig.add_trace(go.Scatter(
            x=df['ds'], 
            y=df['y'], 
            mode='lines', 
            name='Prezzo Reale',
            line=dict(color='blue')
        ))
        
        # Aggiungi forecast
        forecast_future = forecast[forecast['ds'] > df['ds'].max()]
        
        fig.add_trace(go.Scatter(
            x=forecast['ds'], 
            y=forecast['yhat'], 
            mode='lines', 
            name='Previsione',
            line=dict(color='red')
        ))
        
        # Aggiungi intervallo di confidenza
        fig.add_trace(go.Scatter(
            x=forecast['ds'], 
            y=forecast['yhat_upper'],
            fill=None,
            mode='lines',
            line=dict(color='rgba(255,0,0,0.2)'),
            showlegend=False
        ))
        
        fig.add_trace(go.Scatter(
            x=forecast['ds'],
            y=forecast['yhat_lower'],
            fill='tonexty',
            mode='lines',
            line=dict(color='rgba(255,0,0,0.2)'),
            name='Intervallo di Confidenza'
        ))
        
        # Evidenzia la previsione futura
        if not forecast_future.empty:
            fig.add_trace(go.Scatter(
                x=forecast_future['ds'],
                y=forecast_future['yhat'],
                mode='markers',
                name='Previsione Futura',
                marker=dict(size=10, color='green', symbol='star')
            ))
        
        # Aggiorna layout
        title = f"{ticker} - Previsione {'Prossimo Minuto' if interval == '1m' else 'Prossima Ora'}"
        fig.update_layout(
            title=title,
            xaxis_title='Data/Ora',
            yaxis_title='Prezzo (USD)',
            hovermode='x unified',
            template='plotly_white'
        )
        
        fig.show()
    
    def save_predictions(self, filename: str = 'crypto_predictions.csv'):
        """Salva le previsioni in un file CSV"""
        summary = self.get_predictions_summary()
        summary.to_csv(filename, index=False)


def get_crypto_forecasts(tickers = ['BTC-USD', 'ETH-USD', 'BNB-USD']):
    # Crea l'istanza del forecaster
    forecaster = CryptoForecaster(tickers)
    forecaster.download_data(timeframe='both')
    forecaster.forecast_all()
    summary = forecaster.get_predictions_summary()
    return summary.to_string(index=False)
    