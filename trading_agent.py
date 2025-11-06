from openai import OpenAI
from dotenv import load_dotenv
import os
load_dotenv()
# read api key
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
client = OpenAI(api_key=OPENAI_API_KEY)

def previsione_trading_agent(prompt):
    response = client.responses.create(
        model="gpt-5",
        reasoning={"effort": "high"},
        instructions="",
        input=prompt,
    )
    return(response.output_text)