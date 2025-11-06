from indicators import analyze_multiple_tickers
from news_feed import fetch_latest_news
from trading_agent import previsione_trading_agent
from whalealert import format_whale_alerts_to_string
from sentiment import get_sentiment
from forecaster import get_crypto_forecasts

tickers = ['BTC', 'ETH', 'BNB']
indicators_txt = analyze_multiple_tickers(tickers)
news_txt = fetch_latest_news()
whale_alerts_txt = format_whale_alerts_to_string()
sentiment_txt = get_sentiment()
forecasts_txt = get_crypto_forecasts()

msg_info=f"""Indicatori:\n{indicators_txt}\n\n
News:\n{news_txt}\n\n
Whale Alerts:\n{whale_alerts_txt}\n\n
Sentiment:\n{sentiment_txt}\n\n
Forecasts:\n{forecasts_txt}"""

portfolio_data = """- Cash Available: 10000$
- Frozen Cash: 0
- Total Assets: {}
- Current Positions (each shows quantity, avg_cost, current_value, side: LONG/SHORT, leverage):
{}"""

with open('system_prompt.txt', 'r') as f:
    system_prompt = f.read()

system_prompt = system_prompt.format(portfolio_data, msg_info)
print(system_prompt)
print("L'agente sta decidendo la sua azione!")
out = previsione_trading_agent(system_prompt)
print(out)