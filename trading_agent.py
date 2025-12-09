from openai import OpenAI
from dotenv import load_dotenv
import os
import json

load_dotenv()

# DeepSeek API (OpenAI-compatible)
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
client = OpenAI(api_key=DEEPSEEK_API_KEY)

TRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "description": "Type of trading operation to perform",
            "enum": ["open", "close", "hold"]
        },
        "symbol": {
            "type": "string",
            "description": "The cryptocurrency symbol to act on",
            "enum": ["BTC", "ETH", "SOL"]
        },
        "direction": {
            "type": "string",
            "description": "Trade direction: betting the price goes up (long) or down (short).",
            "enum": ["long", "short"]
        },
        "target_portion_of_balance": {
            "type": "number",
            "description": "Fraction of balance/position to allocate/close; from 0.0 to 1.0",
            "minimum": 0,
            "maximum": 1
        },
        "leverage": {
            "type": "number",
            "description": "Leverage multiplier (1-10). Only applicable for 'open'.",
            "minimum": 1,
            "maximum": 10
        },
        "stop_loss_percent": {
            "type": "number",
            "description": "Stop loss distance in % (e.g., 5 = 5%). Default 5% if not specified. Calibrate with ATR.",
            "minimum": 1,
            "maximum": 15
        },
        "take_profit_percent": {
            "type": "number",
            "description": "Take profit distance in % (e.g., 10 = 10%). Optional - if not set, LLM decides when to close.",
            "minimum": 1,
            "maximum": 50
        },
        "reason": {
            "type": "string",
            "description": "Brief explanation of the trading decision (max 300 chars)"
        }
    },
    "required": ["operation", "symbol", "direction", "target_portion_of_balance", "leverage", "reason"],
    "additionalProperties": False
}

def previsione_trading_agent(prompt):
    """Call GPT 5.1 via OpenAI for trading decision.

    Returns:
        tuple: (result_dict, usage_dict) where usage_dict contains token counts
    """

    system_message = """You are a professional crypto trading agent. Analyze the market data and make a trading decision.

You MUST respond with a valid JSON object matching this exact schema:
{
    "operation": "open" | "close" | "hold",
    "symbol": "BTC" | "ETH" | "SOL",
    "direction": "long" | "short",
    "target_portion_of_balance": 0.0 to 1.0,
    "leverage": 1 to 10,
    "stop_loss_percent": 1 to 15 (optional, default 5),
    "take_profit_percent": 1 to 50 (optional),
    "reason": "Brief explanation (max 300 chars)"
}

Rules:
- For "hold" operation, still provide symbol, direction, target_portion_of_balance=0, leverage=1
- leverage is typically 1-10x; only use high leverage (>5x) if you're very confident
- stop_loss_percent: Use ATR to calibrate. Volatile = wider SL (7%), calm = tighter SL (3%). Default 5%.
- take_profit_percent: Optional. If not set, default = 2x SL (risk/reward 1:2). Override for breakouts or near key levels.
- Consider all indicators, sentiment, and news before deciding
- Respond ONLY with the JSON object, no additional text"""

    response = client.chat.completions.create(
        model="gpt-5.1",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_completion_tokens=1000,  # Limitato per controllo costi
        response_format={"type": "json_object"}
    )

    result = json.loads(response.choices[0].message.content)

    # Estrai informazioni sull'utilizzo dei token
    usage = {
        "model": "gpt-5.1",
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }

    return result, usage
