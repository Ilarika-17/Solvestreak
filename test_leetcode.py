import requests

username = "Ilarika_Gautam"  # replace with your real LeetCode username

query = """
query userProfile($username: String!) {
  matchedUser(username: $username) {
    username
    submitStats {
      acSubmissionNum {
        difficulty
        count
      }
    }
    tagProblemCounts {
      advanced {
        tagName
        problemsSolved
      }
      intermediate {
        tagName
        problemsSolved
      }
      fundamental {
        tagName
        problemsSolved
      }
    }
  }
}
"""

response = requests.post(
    "https://leetcode.com/graphql",
    json={"query": query, "variables": {"username": username}},
)

print("Status code:", response.status_code)
print(response.json())