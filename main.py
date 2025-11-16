from indicators import analyze_multiple_tickers
from news_feed import fetch_latest_news
from trading_agent import previsione_trading_agent
from whalealert import format_whale_alerts_to_string
from sentiment import get_sentiment
from forecaster import get_crypto_forecasts
from hyperliquid_trader import HyperLiquidTrader
import os
import json
import db_utils
from dotenv import load_dotenv
load_dotenv()

# Collegamento ad Hyperliquid
TESTNET = True   # True = testnet, False = mainnet (occhio!)
VERBOSE = True    # stampa informazioni extra
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

if not PRIVATE_KEY or not WALLET_ADDRESS:
    raise RuntimeError("PRIVATE_KEY o WALLET_ADDRESS mancanti nel .env")
try:
    bot = HyperLiquidTrader(
        secret_key=PRIVATE_KEY,
        account_address=WALLET_ADDRESS,
        testnet=TESTNET
    )

    # Calcolo delle informazioni in input per Ticker
    tickers = ['BTC', 'ETH', 'SOL']
    indicators_txt, indicators_json  = analyze_multiple_tickers(tickers)
    news_txt = fetch_latest_news()
    # whale_alerts_txt = format_whale_alerts_to_string()
    sentiment_txt, sentiment_json  = get_sentiment()
    forecasts_txt, forecasts_json = get_crypto_forecasts()


    msg_info=f"""<indicatori>\n{indicators_txt}\n</indicatori>\n\n
    <news>\n{news_txt}</news>\n\n
    <sentiment>\n{sentiment_txt}\n</sentiment>\n\n
    <forecast>\n{forecasts_txt}\n</forecast>\n\n"""

    account_status = bot.get_account_status()
    portfolio_data = f"{json.dumps(account_status)}"
    snapshot_id = db_utils.log_account_status(account_status)
    print(f"[db_utils] Operazione inserita con id={snapshot_id}")


    # Creating System prompt
    with open('system_prompt.txt', 'r') as f:
        system_prompt = f.read()
    system_prompt = system_prompt.format(portfolio_data, msg_info)
        
    print("L'agente sta decidendo la sua azione!")
    out = previsione_trading_agent(system_prompt)
    bot.execute_signal(out)


    op_id = db_utils.log_bot_operation(out, system_prompt=system_prompt, indicators=indicators_json, news_text=news_txt, sentiment=sentiment_json, forecasts=forecasts_json)
    print(f"[db_utils] Operazione inserita con id={op_id}")

except Exception as e:
    db_utils.log_error(e, context={"prompt": system_prompt, "tickers": tickers,
                                    "indicators":indicators_json, "news":news_txt,
                                    "sentiment":sentiment_json, "forecasts":forecasts_json,
                                    "balance":account_status
                                    }, source="trading_agent")
    print(f"An error occurred: {e}")