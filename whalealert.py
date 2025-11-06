import requests
from datetime import datetime
import json

def get_whale_alerts():
    """
    Recupera i dati whale alerts e formatta gli alert in modo leggibile
    """
    url = "https://whale-alert.io/data.json?alerts=9&prices=BTC&hodl=bitcoin%2CBTC&potential_profit=bitcoin%2CBTC&average_buy_price=bitcoin%2CBTC&realized_profit=bitcoin%2CBTC&volume=bitcoin%2CBTC&news=true"
    
    try:
        # Fai la richiesta GET
        response = requests.get(url)
        response.raise_for_status()
        
        # Parse JSON
        data = response.json()
        
        # Estrai gli alerts
        alerts = data.get('alerts', [])
        
        if not alerts:
            print("Nessun alert trovato.")
            return
        
        print("🐋 WHALE ALERTS - MOVIMENTI CRYPTO SIGNIFICATIVI 🐋\n")
        print("=" * 80)
        
        for alert in alerts:
            # Parse l'alert string (formato: timestamp,emoji,amount,usd_value,description,link)
            parts = alert.split(',', 5)
            
            if len(parts) >= 6:
                timestamp = parts[0]
                emoji = parts[1]
                amount = parts[2].strip('"')
                usd_value = parts[3].strip('"')
                description = parts[4].strip('"')
                link = parts[5]
                
                # Converti timestamp in data leggibile
                try:
                    dt = datetime.fromtimestamp(int(timestamp))
                    formatted_time = dt.strftime("%d/%m/%Y %H:%M:%S")
                except:
                    formatted_time = "N/A"
                
                # Stampa alert formattato
                print(f"\n{emoji} ALERT del {formatted_time}")
                print(f"💰 Importo: {amount}")
                print(f"💵 Valore USD: {usd_value}")
                print(f"📝 Descrizione: {description}")
                print(f"🔗 Link: {link}")
                print("-" * 80)
        
    except requests.exceptions.RequestException as e:
        print(f"Errore nella richiesta: {e}")
    except json.JSONDecodeError as e:
        print(f"Errore nel parsing JSON: {e}")
    except Exception as e:
        print(f"Errore generico: {e}")

def format_whale_alerts_to_string():
    """
    Versione che ritorna una stringa formattata invece di stampare
    """
    url = "https://whale-alert.io/data.json?alerts=9&prices=BTC&hodl=bitcoin%2CBTC&potential_profit=bitcoin%2CBTC&average_buy_price=bitcoin%2CBTC&realized_profit=bitcoin%2CBTC&volume=bitcoin%2CBTC&news=true"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        alerts = data.get('alerts', [])
        
        if not alerts:
            return "Nessun alert trovato."
        
        result = "🐋 WHALE ALERTS - MOVIMENTI CRYPTO SIGNIFICATIVI 🐋\n\n"
        result += "=" * 80 + "\n"
        
        for alert in alerts:
            parts = alert.split(',', 5)
            
            if len(parts) >= 6:
                timestamp = parts[0]
                emoji = parts[1]
                amount = parts[2].strip('"')
                usd_value = parts[3].strip('"')
                description = parts[4].strip('"')
                link = parts[5]
                
                try:
                    dt = datetime.fromtimestamp(int(timestamp))
                    formatted_time = dt.strftime("%d/%m/%Y %H:%M:%S")
                except:
                    formatted_time = "N/A"
                
                result += f"\n{emoji} ALERT del {formatted_time}\n"
                result += f"💰 Importo: {amount}\n"
                result += f"💵 Valore USD: {usd_value}\n"
                result += f"📝 Descrizione: {description}\n"
                result += "\n"
        
        return result
        
    except Exception as e:
        return f"Errore: {e}"

# Esempio di utilizzo
if __name__ == "__main__":
    # Versione che stampa direttamente
    get_whale_alerts()
    
    # O se preferisci ottenere una stringa
    # formatted_alerts = format_whale_alerts_to_string()
    # print(formatted_alerts)