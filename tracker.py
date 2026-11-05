import requests
import sqlite3
from datetime import datetime

username = "Ilarika_Gautam"

def fetch_leetcode_data(username):
    query = """
    query userProfile($username: String!) {
      matchedUser(username: $username) {
        tagProblemCounts {
          advanced { tagName problemsSolved }
          intermediate { tagName problemsSolved }
          fundamental { tagName problemsSolved }
        }
      }
    }
    """
    response = requests.post(
        "https://leetcode.com/graphql",
        json={"query": query, "variables": {"username": username}},
    )
    data = response.json()["data"]["matchedUser"]["tagProblemCounts"]
    # Flatten all three categories into one list of (topic, count) pairs
    all_topics = data["advanced"] + data["intermediate"] + data["fundamental"]
    return all_topics


def save_to_tracker(topics):
    conn = sqlite3.connect("tracker.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            tag_name TEXT PRIMARY KEY,
            problems_solved INTEGER,
            last_updated TEXT
        )
    """)

    now = datetime.now().isoformat()
    for topic in topics:
        cursor.execute("""
            INSERT INTO topics (tag_name, problems_solved, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(tag_name) DO UPDATE SET
                problems_solved = excluded.problems_solved,
                last_updated = excluded.last_updated
        """, (topic["tagName"], topic["problemsSolved"], now))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    topics = fetch_leetcode_data(username)
    save_to_tracker(topics)
    print(f"Saved {len(topics)}topics to tracker.db")
    conn = sqlite3.connect("tracker.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM topics")
    for row in cursor.fetchall():
        print(row)
    conn.close()