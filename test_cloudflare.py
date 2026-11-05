from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
client = OpenAI(
    base_url=f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
    api_key=os.environ.get("CLOUDFLARE_API_KEY"),
)

response = client.chat.completions.create(
    model=os.environ.get("CLOUDFLARE_MODEL", "@cf/openai/gpt-oss-120b"),
    messages=[{"role": "user", "content": "Say hello and confirm you're working."}],
)
print(response.choices[0].message.content)
