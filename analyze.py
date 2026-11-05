import sqlite3
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


def get_topics_from_tracker():
    conn = sqlite3.connect("tracker.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tag_name, problems_solved FROM topics")
    rows = cursor.fetchall()
    conn.close()
    return rows


def build_prompt(topics):
    topic_lines = "\n".join(f"- {name}: {count} solved" for name, count in topics)
    return f"""Here is a student's LeetCode practice history, showing how many problems they've solved per topic:

{topic_lines}

Based on this real data, identify the 2-3 weakest topics (lowest solve counts relative to the others) and explain briefly why each deserves priority. Then suggest a short, focused practice plan for today - which topic(s) to work on first and roughly how many problems.

Be direct and specific, not generic advice."""


if __name__ == "__main__":
    topics = get_topics_from_tracker()
    prompt = build_prompt(topics)

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
    )
    print(response.text)