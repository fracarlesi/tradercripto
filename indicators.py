import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import ta
from typing import Dict, List, Tuple

class CryptoTechnicalAnalysis:
    """
    Classe per recuperare e calcolare tutti gli indicatori tecnici di una criptovaluta
    """
    
    def __init__(self, exchange_name: str = 'binance'):
        """
        Inizializza la connessione all'exchange
        
        Args:
            exchange_name: Nome dell'exchange (default: binance)
        """
        self.exchange = getattr(ccxt, exchange_name)({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}  # Per i perpetual futures
        })
    
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        """
        Recupera i dati OHLCV dall'exchange
        
        Args:
            symbol: Simbolo della cripto (es. 'BTC/USDT')
            timeframe: Timeframe (es. '1m', '4h')
            limit: Numero di candele da recuperare
            
        Returns:
            DataFrame con i dati OHLCV
        """
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    
    def calculate_ema(self, data: pd.Series, period: int) -> pd.Series:
        """Calcola la EMA (Exponential Moving Average)"""
        return ta.trend.EMAIndicator(data, window=period).ema_indicator()
    
    def calculate_macd(self, data: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Calcola il MACD"""
        macd = ta.trend.MACD(data)
        return macd.macd(), macd.macd_signal(), macd.macd_diff()
    
    def calculate_rsi(self, data: pd.Series, period: int) -> pd.Series:
        """Calcola il RSI"""
        return ta.momentum.RSIIndicator(data, window=period).rsi()
    
    def calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
        """Calcola l'ATR (Average True Range)"""
        return ta.volatility.AverageTrueRange(high, low, close, window=period).average_true_range()
    
    def get_funding_rate(self, symbol: str) -> float:
        """
        Recupera il funding rate per i perpetual futures
        
        Args:
            symbol: Simbolo della cripto (es. 'BTC/USDT')
            
        Returns:
            Funding rate attuale
        """
        try:
            funding = self.exchange.fetch_funding_rate(symbol)
            return funding['fundingRate']
        except Exception as e:
            print(f"Errore nel recupero del funding rate: {e}")
            return 0.0
    
    def get_open_interest(self, symbol: str, timeframe: str = '5m', limit: int = 100) -> Dict[str, float]:
        """
        Recupera l'Open Interest
        
        Args:
            symbol: Simbolo della cripto (es. 'BTC/USDT')
            timeframe: Timeframe per calcolare la media
            limit: Numero di dati storici per la media
            
        Returns:
            Dict con latest e average open interest
        """
        try:
            oi_history = self.exchange.fetch_open_interest_history(symbol, timeframe, limit=limit)
            if oi_history:
                latest_oi = oi_history[-1]['openInterestValue']
                avg_oi = np.mean([x['openInterestValue'] for x in oi_history])
                return {'latest': latest_oi, 'average': avg_oi}
        except Exception as e:
            print(f"Errore nel recupero dell'open interest: {e}")
        
        return {'latest': 0.0, 'average': 0.0}
    
    def get_complete_analysis(self, ticker: str) -> Dict:
        """
        Recupera tutti gli indicatori tecnici per un ticker
        
        Args:
            ticker: Ticker della criptovaluta (es. 'BTC', 'ETH')
            
        Returns:
            Dizionario completo con tutti gli indicatori
        """
        # Prepara il simbolo per l'exchange
        symbol = f"{ticker}/USDT"
        
        print(f"Recupero dati per {symbol}...")
        
        # 1. DATI INTRADAY (1 minuto) - ultimi 10 minuti
        df_1m = self.fetch_ohlcv(symbol, '1m', limit=100)
        
        # Calcola gli indicatori per l'intraday
        df_1m['ema_20'] = self.calculate_ema(df_1m['close'], 20)
        macd_line, signal_line, macd_diff = self.calculate_macd(df_1m['close'])
        df_1m['macd'] = macd_diff  # MACD histogram
        df_1m['rsi_7'] = self.calculate_rsi(df_1m['close'], 7)
        df_1m['rsi_14'] = self.calculate_rsi(df_1m['close'], 14)
        
        # Prendi gli ultimi 10 dati
        last_10 = df_1m.tail(10)
        
        # 2. DATI 4H per contesto long-term
        df_4h = self.fetch_ohlcv(symbol, '4h', limit=100)
        df_4h['ema_20'] = self.calculate_ema(df_4h['close'], 20)
        df_4h['ema_50'] = self.calculate_ema(df_4h['close'], 50)
        df_4h['atr_3'] = self.calculate_atr(df_4h['high'], df_4h['low'], df_4h['close'], 3)
        df_4h['atr_14'] = self.calculate_atr(df_4h['high'], df_4h['low'], df_4h['close'], 14)
        macd_4h, _, macd_diff_4h = self.calculate_macd(df_4h['close'])
        df_4h['macd'] = macd_diff_4h
        df_4h['rsi_14'] = self.calculate_rsi(df_4h['close'], 14)
        
        # Calcola volume medio
        avg_volume = df_4h['volume'].tail(20).mean()
        
        last_10_4h = df_4h.tail(10)
        
        # 3. OPEN INTEREST E FUNDING RATE
        oi_data = self.get_open_interest(symbol)
        funding_rate = self.get_funding_rate(symbol)
        
        # 4. VALORI CORRENTI
        current_data = df_1m.iloc[-1]
        current_4h = df_4h.iloc[-1]
        
        # 5. COMPILA IL RISULTATO
        result = {
            'ticker': ticker,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            
            # DATI CORRENTI
            'current': {
                'price': current_data['close'],
                'ema20': current_data['ema_20'],
                'macd': current_data['macd'],
                'rsi_7': current_data['rsi_7']
            },
            
            # OPEN INTEREST E FUNDING
            'derivatives': {
                'open_interest_latest': oi_data['latest'],
                'open_interest_average': oi_data['average'],
                'funding_rate': funding_rate
            },
            
            # SERIE INTRADAY (minuto per minuto)
            'intraday': {
                'mid_prices': last_10['close'].tolist(),
                'ema_20': last_10['ema_20'].tolist(),
                'macd': last_10['macd'].tolist(),
                'rsi_7': last_10['rsi_7'].tolist(),
                'rsi_14': last_10['rsi_14'].tolist()
            },
            
            # CONTESTO 4H
            'longer_term_4h': {
                'ema_20_current': current_4h['ema_20'],
                'ema_50_current': current_4h['ema_50'],
                'atr_3_current': current_4h['atr_3'],
                'atr_14_current': current_4h['atr_14'],
                'volume_current': current_4h['volume'],
                'volume_average': avg_volume,
                'macd_series': last_10_4h['macd'].tolist(),
                'rsi_14_series': last_10_4h['rsi_14'].tolist()
            }
        }
        
        return result
    
    def format_output(self, data: Dict) -> str:
        """
        Formatta l'output in modo leggibile come nell'esempio
        
        Args:
            data: Dizionario con tutti i dati
            
        Returns:
            Stringa formattata
        """
        output = f"\n{'='*80}\n"
        output += f"ALL {data['ticker']} DATA\n"
        output += f"Timestamp: {data['timestamp']}\n"
        output += f"{'='*80}\n\n"
        
        # DATI CORRENTI
        curr = data['current']
        output += f"current_price = {curr['price']:.1f}, "
        output += f"current_ema20 = {curr['ema20']:.3f}, "
        output += f"current_macd = {curr['macd']:.3f}, "
        output += f"current_rsi (7 period) = {curr['rsi_7']:.3f}\n\n"
        
        # DERIVATIVES
        deriv = data['derivatives']
        output += f"In addition, here is the latest {data['ticker']} open interest and funding rate for perps:\n"
        output += f"Open Interest: Latest: {deriv['open_interest_latest']:.2f} "
        output += f"Average: {deriv['open_interest_average']:.2f}\n"
        output += f"Funding Rate: {deriv['funding_rate']:.2e}\n\n"
        
        # INTRADAY SERIES
        intra = data['intraday']
        output += f"Intraday series (by minute, oldest → latest):\n"
        output += f"Mid prices: {[round(x, 1) for x in intra['mid_prices']]}\n"
        output += f"EMA indicators (20‑period): {[round(x, 3) for x in intra['ema_20']]}\n"
        output += f"MACD indicators: {[round(x, 3) for x in intra['macd']]}\n"
        output += f"RSI indicators (7‑Period): {[round(x, 3) for x in intra['rsi_7']]}\n"
        output += f"RSI indicators (14‑Period): {[round(x, 3) for x in intra['rsi_14']]}\n\n"
        
        # LONGER TERM
        lt = data['longer_term_4h']
        output += f"Longer‑term context (4‑hour timeframe):\n"
        output += f"20‑Period EMA: {lt['ema_20_current']:.3f} vs. "
        output += f"50‑Period EMA: {lt['ema_50_current']:.3f}\n"
        output += f"3‑Period ATR: {lt['atr_3_current']:.3f} vs. "
        output += f"14‑Period ATR: {lt['atr_14_current']:.3f}\n"
        output += f"Current Volume: {lt['volume_current']:.3f} vs. "
        output += f"Average Volume: {lt['volume_average']:.3f}\n"
        output += f"MACD indicators: {[round(x, 3) for x in lt['macd_series']]}\n"
        output += f"RSI indicators (14‑Period): {[round(x, 3) for x in lt['rsi_14_series']]}\n"
        
        output += f"\n{'='*80}\n"
        
        return output


def main():
    """Funzione principale per testare il sistema"""
    
    # Crea l'analizzatore
    analyzer = CryptoTechnicalAnalysis(exchange_name='binance')
    
    # Lista di ticker da analizzare
    tickers = ['BTC', 'ETH', 'SOL', 'BNB', 'DOGE', 'XRP']  # Aggiungi altri ticker se necessario
    
    for ticker in tickers:
        try:
            # Ottieni l'analisi completa
            data = analyzer.get_complete_analysis(ticker)
            
            # Stampa l'output formattato
            print(analyzer.format_output(data))
            
            # Salva anche in formato JSON se necessario
            import json
            with open(f'{ticker}_analysis.json', 'w') as f:
                json.dump(data, f, indent=2)
            
        except Exception as e:
            print(f"Errore nell'analisi di {ticker}: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()