from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
client = OpenAI(
    base_url="https://api.sambanova.ai/v1/",
    api_key=os.environ.get("SAMBANOVA_API_KEY"),
)

response = client.chat.completions.create(
    model=os.environ.get("SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct"),
    messages=[{"role": "user", "content": "Say hello and confirm you're working."}],
)
print(response.choices[0].message.content)
