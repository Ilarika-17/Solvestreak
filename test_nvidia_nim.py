from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ.get("NVIDIA_NIM_API_KEY"),
)

response = client.chat.completions.create(
    model=os.environ.get("NVIDIA_NIM_MODEL", "openai/gpt-oss-20b"),
    messages=[{"role": "user", "content": "Say hello and confirm you're working."}],
)
print(response.choices[0].message.content)
