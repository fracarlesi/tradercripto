import requests
import time
import os
import json
# load dotenv
from dotenv import load_dotenv
load_dotenv()


# --- Configurazione ---

# Endpoint dell'API come da documentazione
API_URL = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"

# La tua API Key (Best practice: impostala come variabile d'ambiente)
# NON SCRIVERE LA TUA KEY DIRETTAMENTE NEL CODICE
API_KEY = os.getenv('CMC_PRO_API_KEY') 

# Intervallo per il tuo trading bot (3 minuti * 60 secondi)
INTERVALLO_SECONDI = 3 * 60 

# --- Funzione per chiamare l'API ---

def get_latest_fear_and_greed():
    """
    Chiama l'API di CoinMarketCap per ottenere l'ultimo valore 
    del Fear & Greed Index.
    """
    if not API_KEY:
        print("Errore: La variabile d'ambiente CMC_PRO_API_KEY non è impostata.")
        return None

    # Header per l'autenticazione
    headers = {
      'Accepts': 'application/json',
      'X-CMC_PRO_API_KEY': API_KEY,
    }

    # Parametri della richiesta
    # Vogliamo solo il valore più recente, quindi limit=1
    parameters = {
      'limit': 1 
    }

    try:
        # Esegui la chiamata GET
        response = requests.get(API_URL, headers=headers, params=parameters)
        
        # Controlla se la richiesta ha avuto successo (es. 200 OK)
        response.raise_for_status() 

        data = response.json()

        # Estrai i dati più recenti (è una lista, prendiamo il primo elemento)
        if data and 'data' in data and len(data['data']) > 0:
            latest_record = data['data'][0]
            valore = latest_record.get('value')
            classificazione = latest_record.get('value_classification')
            timestamp = latest_record.get('timestamp')
            
            return {
                "valore": valore,
                "classificazione": classificazione,
                "timestamp": timestamp
            }
        else:
            print("Errore: La risposta JSON non contiene i dati attesi.")
            return None

    except requests.exceptions.HTTPError as errh:
        print(f"Errore HTTP: {errh}")
    except requests.exceptions.ConnectionError as errc:
        print(f"Errore di Connessione: {errc}")
    except requests.exceptions.Timeout as errt:
        print(f"Errore di Timeout: {errt}")
    except requests.exceptions.RequestException as err:
        print(f"Errore generico della richiesta: {err}")
    
    return None

# --- Loop Principale del Trading Agent ---

if __name__ == "__main__":
    print("Avvio del Trading Agent (Sentiment Analysis)...")
    print("\n--- Inizio ciclo (ogni 3 minuti) ---")
    print("Recupero il sentiment del mercato (Fear & Greed Index)...")
    
    sentiment = get_latest_fear_and_greed()
    
    if sentiment:
        print(f"Sentiment attuale ({sentiment['timestamp']}):")
        print(f"  Valore: {sentiment['valore']}")
        print(f"  Classificazione: {sentiment['classificazione']}")
    else:
        print("Impossibile recuperare il sentiment. Riprovo nel prossimo ciclo.")