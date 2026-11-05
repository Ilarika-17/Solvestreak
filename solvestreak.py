import requests
import sqlite3
import os
import json
import random
import time
import httpx
import concurrent.futures
from datetime import datetime, date, timedelta, timezone
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import auth

load_dotenv(override=True)

GEMINI_API_KEY_BACKUP = os.environ.get("GEMINI_API_KEY_BACKUP")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL")  # no default - see setup notes

# Two more free-tier fallback providers, added purely to stretch the shared
# daily AI budget further once real users are on the app -- neither costs
# anything and neither requires a card on file to sign up for (unlike
# Cerebras/Together/Mistral, which now all require a payment method just to
# get a key). Both are last-resort: see generate_with_retry for the order
# they're tried in. Each is entirely optional -- the app runs fine with
# neither configured, exactly like OpenRouter today.
SAMBANOVA_API_KEY = os.environ.get("SAMBANOVA_API_KEY")
# SambaNova's free tier (no payment method on the account) is 20 requests/day
# -- small, but it's 20 extra, totally free, zero-setup-friction requests on
# top of everything else. Matches Groq's default model quality tier.
#
# Stick with this default rather than swapping in another "production"
# model from SambaNova's own model list -- confirmed by hand that
# DeepSeek-V3.1, despite being listed alongside this one as a free-tier
# model, actually returns 402 PAYMENT_METHOD_REQUIRED without a card on
# file. Meta-Llama-3.3-70B-Instruct is the one verified to work with no
# payment method at all (it can still return a transient 429 when busy --
# that's normal demand, not a payment wall, and generate_with_retry already
# retries it before moving on).
SAMBANOVA_MODEL = os.environ.get("SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct")

CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_KEY = os.environ.get("CLOUDFLARE_API_KEY")
# Cloudflare Workers AI's free tier is metered in "Neurons" (10,000/day, no
# card needed for the account), not a flat request count -- bigger models
# burn through that budget faster. gpt-oss-120b is Cloudflare-hosted (not a
# "proxied" model billed straight to another provider) and matches the
# quality of the gpt-oss-120b model Groq already uses here.
CLOUDFLARE_MODEL = os.environ.get("CLOUDFLARE_MODEL", "@cf/openai/gpt-oss-120b")

NVIDIA_NIM_API_KEY = os.environ.get("NVIDIA_NIM_API_KEY")
# NVIDIA NIM's free tier (build.nvidia.com / integrate.api.nvidia.com) is by
# far the biggest of the "extra" fallback providers here -- 40 requests/min
# and 10,000 requests/day per account, no card needed (an NVIDIA Developer
# Program account with phone verification is required instead). Placed last
# in the chain for now, same as every other freshly-added provider, simply
# because it hasn't been verified end-to-end by hand yet the way Cloudflare
# and SambaNova have -- its real daily budget is bigger than everything else
# in this chain combined, so it's worth promoting earlier once it's
# confirmed solid in practice.
#
# NOTE: "meta/llama-3.3-70b-instruct" (NVIDIA's own documented default model
# ID) is no longer listed as a Free Endpoint in the live build.nvidia.com
# catalog as of 2026-10 -- it returns 0 search results. Using
# "openai/gpt-oss-20b" instead, confirmed available as a Free Endpoint and
# already proven reliable in this app via Groq (GROQ_MODEL=openai/gpt-oss-20b).
NVIDIA_NIM_MODEL = os.environ.get("NVIDIA_NIM_MODEL", "openai/gpt-oss-20b")

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
client_backup = genai.Client(api_key=GEMINI_API_KEY_BACKUP) if GEMINI_API_KEY_BACKUP else None

groq_client = None
if GROQ_API_KEY:
    from groq import Groq
    groq_client = Groq(api_key=GROQ_API_KEY)

openrouter_client = None
if OPENROUTER_API_KEY and OPENROUTER_MODEL:
    from openai import OpenAI
    openrouter_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)

sambanova_client = None
if SAMBANOVA_API_KEY:
    from openai import OpenAI
    sambanova_client = OpenAI(base_url="https://api.sambanova.ai/v1/", api_key=SAMBANOVA_API_KEY)

cloudflare_client = None
if CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_KEY:
    from openai import OpenAI
    cloudflare_client = OpenAI(
        base_url=f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        api_key=CLOUDFLARE_API_KEY,
    )

nvidia_nim_client = None
if NVIDIA_NIM_API_KEY:
    from openai import OpenAI
    nvidia_nim_client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_NIM_API_KEY)

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


class QuotaExceededError(Exception):
    """Raised when the Gemini API reports the daily/rate quota is exhausted (HTTP 429)."""
    pass


class ModelOverloadedError(Exception):
    """Raised when the Gemini API stays overloaded (HTTP 503) after all retries are exhausted."""
    pass


class NetworkError(Exception):
    """Raised when Gemini can't be reached at all (no internet / DNS failure /
    firewall), as opposed to Gemini itself returning an error. Usually means
    the connection dropped or DNS couldn't resolve at that moment, not that
    anything is wrong with the app itself."""
    pass


class CalendarNotConnectedError(Exception):
    """Raised when this user has no usable Google Calendar credentials --
    either they signed in before Calendar access was requested during
    sign-in, or their stored refresh token stopped working. Callers should
    treat this as "no calendar for this person yet", not a real error."""
    pass


SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

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
    return data["advanced"] + data["intermediate"] + data["fundamental"]


def save_to_tracker(topics, user_id):
    # user_topics (not the older single-user "topics" table) so each
    # person's LeetCode tag counts are kept separate once multiple people
    # use this app. This cache is fully rebuilt from the LeetCode API on
    # every dashboard load, so there's nothing to migrate from "topics".
    conn = sqlite3.connect("tracker.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_topics (
            user_id INTEGER,
            tag_name TEXT,
            problems_solved INTEGER,
            last_updated TEXT,
            PRIMARY KEY (user_id, tag_name)
        )
    """)
    # Append-only snapshot history, one row per sync per topic -- unlike
    # user_topics above (which is overwritten on every sync and only ever
    # reflects "right now"), this keeps every past value so a real
    # solved-count trend over time becomes possible once enough syncs have
    # accumulated. Nothing reads this yet; it exists so that data isn't
    # lost starting from today.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_topics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            tag_name TEXT,
            problems_solved INTEGER,
            recorded_at TEXT
        )
    """)
    now = datetime.now().isoformat()
    for topic in topics:
        cursor.execute("""
            INSERT INTO user_topics (user_id, tag_name, problems_solved, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, tag_name) DO UPDATE SET
                problems_solved = excluded.problems_solved,
                last_updated = excluded.last_updated
        """, (user_id, topic["tagName"], topic["problemsSolved"], now))
        cursor.execute("""
            INSERT INTO user_topics_history (user_id, tag_name, problems_solved, recorded_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, topic["tagName"], topic["problemsSolved"], now))
    conn.commit()
    conn.close()


def get_topics_from_tracker(user_id):
    conn = sqlite3.connect("tracker.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_topics (
            user_id INTEGER,
            tag_name TEXT,
            problems_solved INTEGER,
            last_updated TEXT,
            PRIMARY KEY (user_id, tag_name)
        )
    """)
    cursor.execute("SELECT tag_name, problems_solved FROM user_topics WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_weak_topics(topics, n=3):
    sorted_topics = sorted(topics, key=lambda t: t[1])
    return [name for name, count in sorted_topics[:n]]


def _call_gemini(gemini_client, prompt, max_retries=3, base_delay=15):
    for attempt in range(1, max_retries + 1):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return response.text
        except ClientError as e:
            if getattr(e, "code", None) == 429:
                raise QuotaExceededError("Gemini quota exhausted for this key.") from e
            raise
        except ServerError as e:
            if attempt == max_retries:
                raise ModelOverloadedError("Gemini is still overloaded after retries.") from e
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Gemini busy (attempt {attempt}/{max_retries}) - retrying in {delay}s...")
            time.sleep(delay)
        except httpx.TransportError as e:
            # Couldn't even reach Gemini's servers (DNS failure, no internet,
            # connection dropped, etc.) -- this is a local connectivity issue,
            # not Gemini being down, so a short retry and a clear message is
            # more useful than a raw traceback.
            if attempt == max_retries:
                raise NetworkError(
                    "Couldn't reach Gemini. Check your internet connection and try again."
                ) from e
            delay = min(base_delay, 5) * attempt
            print(f"Network error reaching Gemini (attempt {attempt}/{max_retries}): {e} - retrying in {delay}s...")
            time.sleep(delay)


def _call_groq(prompt, max_retries=3, base_delay=10):
    # Groq's JSON mode requires the literal word "json" to appear in the
    # message text, not just a structure that looks like JSON.
    groq_prompt = prompt + "\n\nRespond with valid JSON only, matching the structure above exactly."
    for attempt in range(1, max_retries + 1):
        try:
            completion = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": groq_prompt}],
                response_format={"type": "json_object"},
                # Raised from 8192 -- a big Custom Notes batch (up to 30
                # topics, each with up to 7 rich fields) or a large Custom
                # Paper can produce more JSON than that, and a response cut
                # off mid-stream fails to parse, wasting the whole call.
                max_tokens=16384,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if attempt == max_retries:
                raise ModelOverloadedError(f"Groq backup also failed: {e}") from e
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Groq busy (attempt {attempt}/{max_retries}) - retrying in {delay}s...")
            time.sleep(delay)


def _call_openrouter(prompt, max_retries=3, base_delay=10):
    # Same JSON-keyword requirement as Groq applies broadly across
    # OpenAI-compatible JSON-mode APIs, so include it here too.
    openrouter_prompt = prompt + "\n\nRespond with valid JSON only, matching the structure above exactly."
    for attempt in range(1, max_retries + 1):
        try:
            completion = openrouter_client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[{"role": "user", "content": openrouter_prompt}],
                response_format={"type": "json_object"},
                max_tokens=16384,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if attempt == max_retries:
                raise ModelOverloadedError(f"OpenRouter backup also failed: {e}") from e
            delay = base_delay * (2 ** (attempt - 1))
            print(f"OpenRouter busy (attempt {attempt}/{max_retries}) - retrying in {delay}s...")
            time.sleep(delay)


def _call_sambanova(prompt, max_retries=3, base_delay=10):
    # Same OpenAI-compatible JSON-mode pattern as Groq/OpenRouter.
    sambanova_prompt = prompt + "\n\nRespond with valid JSON only, matching the structure above exactly."
    for attempt in range(1, max_retries + 1):
        try:
            completion = sambanova_client.chat.completions.create(
                model=SAMBANOVA_MODEL,
                messages=[{"role": "user", "content": sambanova_prompt}],
                response_format={"type": "json_object"},
                max_tokens=16384,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if attempt == max_retries:
                raise ModelOverloadedError(f"SambaNova backup also failed: {e}") from e
            delay = base_delay * (2 ** (attempt - 1))
            print(f"SambaNova busy (attempt {attempt}/{max_retries}) - retrying in {delay}s...")
            time.sleep(delay)


def _call_cloudflare(prompt, max_retries=3, base_delay=10):
    # Same OpenAI-compatible JSON-mode pattern as Groq/OpenRouter/SambaNova.
    cloudflare_prompt = prompt + "\n\nRespond with valid JSON only, matching the structure above exactly."
    for attempt in range(1, max_retries + 1):
        try:
            completion = cloudflare_client.chat.completions.create(
                model=CLOUDFLARE_MODEL,
                messages=[{"role": "user", "content": cloudflare_prompt}],
                response_format={"type": "json_object"},
                max_tokens=16384,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if attempt == max_retries:
                raise ModelOverloadedError(f"Cloudflare Workers AI backup also failed: {e}") from e
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Cloudflare Workers AI busy (attempt {attempt}/{max_retries}) - retrying in {delay}s...")
            time.sleep(delay)


def _call_nvidia_nim(prompt, max_retries=3, base_delay=10):
    # Same OpenAI-compatible JSON-mode pattern as Groq/OpenRouter/SambaNova/Cloudflare.
    nvidia_prompt = prompt + "\n\nRespond with valid JSON only, matching the structure above exactly."
    for attempt in range(1, max_retries + 1):
        try:
            completion = nvidia_nim_client.chat.completions.create(
                model=NVIDIA_NIM_MODEL,
                messages=[{"role": "user", "content": nvidia_prompt}],
                response_format={"type": "json_object"},
                max_tokens=16384,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if attempt == max_retries:
                raise ModelOverloadedError(f"NVIDIA NIM backup also failed: {e}") from e
            delay = base_delay * (2 ** (attempt - 1))
            print(f"NVIDIA NIM busy (attempt {attempt}/{max_retries}) - retrying in {delay}s...")
            time.sleep(delay)


def generate_with_retry(prompt):
    """
    Tries, in order: Groq (primary, if configured) -> Gemini primary key ->
    NVIDIA NIM (if configured) -> OpenRouter (if configured) -> Cloudflare
    Workers AI (if configured) -> backup Gemini key (if configured) ->
    SambaNova (if configured). Returns the raw JSON text from whichever one
    succeeds first and actually parses as valid JSON. Only raises if every
    configured option is exhausted.

    NVIDIA NIM was promoted to 3rd (confirmed working end-to-end, and its
    published free budget -- 10,000 requests/day -- is the largest of any
    provider in this whole chain). The Gemini backup key was moved to after
    Cloudflare, so the two independent Gemini keys aren't tried back-to-back
    -- other providers get a shot first if the primary Gemini key is having
    a bad day. SambaNova stays last -- confirmed by hand that its free-tier
    capacity for the one model that doesn't require a payment method
    (Meta-Llama-3.3-70B-Instruct; see SAMBANOVA_MODEL above) is frequently
    oversubscribed (repeated 429 "high demand" responses).
    """
    providers = []
    if groq_client:
        providers.append(("Groq (primary)", lambda: _call_groq(prompt)))
    providers.append((
        "Gemini (backup provider)" if groq_client else "Gemini (primary)",
        lambda: _call_gemini(client, prompt),
    ))
    if nvidia_nim_client:
        providers.append(("NVIDIA NIM (backup provider)", lambda: _call_nvidia_nim(prompt)))
    if openrouter_client:
        providers.append(("OpenRouter (backup provider)", lambda: _call_openrouter(prompt)))
    if cloudflare_client:
        providers.append(("Cloudflare Workers AI (backup provider)", lambda: _call_cloudflare(prompt)))
    if client_backup:
        providers.append(("Gemini (backup key)", lambda: _call_gemini(client_backup, prompt)))
    if sambanova_client:
        providers.append(("SambaNova (backup provider)", lambda: _call_sambanova(prompt)))

    last_error = None
    for name, call in providers:
        try:
            print(f"Trying {name}...")
            result = call()
            json.loads(result, strict=False)  # make sure it's actually usable before accepting it
            return result
        except (QuotaExceededError, ModelOverloadedError, NetworkError) as e:
            print(f"{name} unavailable: {e}")
            last_error = e
            continue
        except (json.JSONDecodeError, ValueError) as e:
            print(f"{name} returned invalid JSON, trying next provider: {e}")
            last_error = ModelOverloadedError(f"{name} returned invalid JSON: {e}")
            continue

    raise last_error


def parse_json_response(response_text):
    return json.loads(response_text, strict=False)


def generate_practice_paper(weak_topics):
    if DRY_RUN:
        return {
            "problems": [
                {
                    "id": i + 1,
                    "topic": topic,
                    "difficulty": "Easy",
                    "title": f"[DRY RUN] Sample Problem for {topic}",
                    "statement": f"[DRY RUN] This is a placeholder problem statement for {topic}.",
                }
                for i, topic in enumerate(weak_topics)
            ]
        }
    topics_str = ", ".join(weak_topics)
    prompt = f"""Create a practice paper with 4 original coding problems targeting these weak topics: {topics_str}.

Each problem must be an ORIGINAL problem you write yourself, not copied from LeetCode or any known problem set. Write it like a real interview-style question: a clear problem statement, the input/output format, and one worked example.

Respond with this exact structure:
{{
  "problems": [
    {{
      "id": 1,
      "topic": "topic name",
      "difficulty": "Easy or Medium",
      "title": "short title",
      "statement": "full problem statement including example input and output"
    }}
  ]
}}"""
    response = generate_with_retry(prompt)
    return parse_json_response(response)


def generate_solutions(practice_paper):
    if DRY_RUN:
        return {
            "solutions": [
                {
                    "id": p["id"],
                    "approach": f"[DRY RUN] Placeholder approach for problem {p['id']}.",
                    "code": "# [DRY RUN] placeholder code",
                    "complexity": "[DRY RUN] O(n) time, O(1) space",
                }
                for p in practice_paper["problems"]
            ]
        }
    problems_str = "\n\n".join(
        f"Problem {p['id']} ({p['topic']}, {p['difficulty']}): {p['title']}\n{p['statement']}"
        for p in practice_paper["problems"]
    )
    prompt = f"""Here are coding problems. For each one, provide: a plain-language explanation of the approach, working Python code, and the time/space complexity.

{problems_str}

Respond with this exact structure:
{{
  "solutions": [
    {{
      "id": 1,
      "approach": "explanation of the approach in plain language",
      "code": "python code as a single string",
      "complexity": "time and space complexity"
    }}
  ]
}}"""
    response = generate_with_retry(prompt)
    return parse_json_response(response)


_CUSTOM_TYPE_LABELS = {
    "mcq": "multiple-choice question(s)",
    "short": "short-answer question(s)",
    "long": "long-answer question(s)",
}
# Fixed order questions always appear in on the page, regardless of the
# order the checkboxes were ticked in on the form.
_CUSTOM_TYPE_ORDER = ["mcq", "short", "long"]

# Which mark values are pickable for each non-mcq type, in the order they're
# shown on the form and generated in. MCQ isn't broken down by marks on the
# form -- it's a single flat count, worth 1 mark each for the paper's total.
_CUSTOM_MARKS_BY_TYPE = {
    "short": [1, 2, 3],
    "long": [4, 5, 6, 7, 10],
}

# Caps how many (type, marks) bucket calls run at once. Before marks existed,
# a custom paper had at most 3 buckets (mcq/short/long), so firing one
# thread per bucket meant at most 3 concurrent API calls. Now that short/
# long are split per individual mark value, a paper with several mark
# values ticked can have far more buckets -- up to 1 mcq + 3 short + 5 long
# = 9 -- and firing that many requests at once risks tripping the AI
# provider's own per-minute/concurrent rate limit, which fails the WHOLE
# generation (generate_custom_paper_and_solutions re-raises the first
# bucket's error) even though most buckets would have succeeded on their
# own. Capping concurrency keeps the common case (a handful of buckets)
# exactly as fast as before while keeping worst-case concurrency sane --
# extra buckets beyond the cap just run in a second wave instead of all at
# once.
_MAX_CONCURRENT_BUCKET_CALLS = 4

_CUSTOM_DIFFICULTIES = ("easy", "medium", "hard")

_CUSTOM_DIFFICULTY_BRIEF = {
    "easy": (
        "EASY -- fundamental, direct recall and single-step application questions. "
        "Multiple-choice options should be clearly distinguishable (no closely-reasoned trick distractors). "
        "Numerical questions should involve one straightforward calculation. Coding questions should use short, "
        "common, well-known patterns."
    ),
    "medium": (
        "MEDIUM -- the moderate, multi-step reasoning and application questions typical of a standard course exam. "
        "Numerical questions may need 2-3 calculation steps. Coding questions may involve tracing a short loop/"
        "function or a function with a couple of edge cases."
    ),
    "hard": (
        "HARD -- advanced, multi-step, conceptually demanding questions that often combine two or more ideas, or "
        "require careful reasoning to avoid a plausible-looking wrong answer. Multiple-choice distractors should be "
        "closely reasoned, not obviously wrong. Numerical questions should require multiple chained calculation "
        "steps. Coding questions may involve tracing more intricate logic, spotting a subtle bug, or reasoning "
        "about an edge case or complexity trade-off."
    ),
}


def _duration_label(minutes):
    """
    Turns a raw minute count (as picked on the Custom Practice Paper form)
    into a human label for both the generation prompt and, in history.py's
    own copy of this helper, the paper's printed cover page -- e.g. 90 ->
    "1 hour 30 minutes", 60 -> "1 hour", 45 -> "45 minutes".
    """
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 60
    if minutes <= 0:
        minutes = 60
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if mins:
        parts.append(f"{mins} minute" + ("s" if mins != 1 else ""))
    return " ".join(parts) if parts else "0 minutes"


def _shuffled_topic_sequence(topics, total):
    """
    Builds a length-`total` list of topics for the DRY_RUN placeholder path,
    drawn from repeated shuffled passes over `topics` rather than a plain
    `topics[i % len(topics)]` cycle -- the old modulo cycle always produced
    the exact same topic-per-question-slot mapping for a given question
    count, which is what "shuffled" is meant to fix. Re-shuffles each time
    the pool runs out, so a short list of topics doesn't visibly repeat the
    same order pass after pass.
    """
    if not topics:
        topics = ["General"]
    sequence = []
    pool = []
    while len(sequence) < total:
        if not pool:
            pool = topics[:]
            random.shuffle(pool)
        sequence.append(pool.pop())
    return sequence


def _iter_custom_buckets(counts):
    """
    Yields (qtype, marks, n) for every non-zero bucket in `counts`, in a
    fixed order: mcq, then short 1/2/3, then long 4/5/6/7/10. `marks` is
    None for mcq -- it isn't broken down by marks, it's worth a flat 1 mark
    each.

    Handles both the current nested shape --
    {"mcq": 5, "short": {"1": 2, "3": 4}, "long": {"10": 1}} -- and the
    flat shape from before the marks breakdown existed --
    {"mcq": 5, "short": 6, "long": 4}. The flat shape only shows up when
    reading `counts` back out of a custom paper saved to History before
    this feature existed -- a fresh generation request always sends the
    nested shape. A legacy "short"/"long" bucket comes back with
    marks=None, same as mcq, since there's no mark value to recover for it.
    """
    mcq_n = counts.get("mcq")
    if isinstance(mcq_n, (int, float)) and mcq_n > 0:
        yield ("mcq", None, int(mcq_n))

    for qtype, allowed_marks in _CUSTOM_MARKS_BY_TYPE.items():
        bucket = counts.get(qtype)
        if isinstance(bucket, dict):
            for marks in allowed_marks:
                n = bucket.get(str(marks), bucket.get(marks))
                if isinstance(n, (int, float)) and n > 0:
                    yield (qtype, marks, int(n))
        elif isinstance(bucket, (int, float)) and bucket > 0:
            # Legacy flat count from before marks existed.
            yield (qtype, None, int(bucket))


def _bucket_label(qtype, marks):
    """Human label for one (qtype, marks) bucket, e.g. "2-mark short-answer
    question(s)" -- or, for mcq or a legacy marks=None bucket, just the
    plain _CUSTOM_TYPE_LABELS text."""
    if marks is None:
        return _CUSTOM_TYPE_LABELS[qtype]
    base = "short-answer" if qtype == "short" else "long-answer"
    return f"{marks}-mark {base} question(s)"


def _marks_depth_guidance(marks):
    """
    How much depth a question worth `marks` marks should expect in its
    answer -- replaces the old flat "short: 2-4 sentences, long: detailed
    multi-paragraph" instruction now that the person picks an exact mark
    value per question instead of just "short" or "long".
    """
    if marks <= 1:
        return "Worth 1 mark -- keep the expected answer extremely brief: a single fact, term, or one-line calculation result."
    if marks == 2:
        return "Worth 2 marks -- expect a brief 1-2 sentence answer, or a quick single/two-step calculation."
    if marks == 3:
        return "Worth 3 marks -- expect a short paragraph (2-4 sentences), or a calculation with a few steps shown."
    if marks <= 5:
        return f"Worth {marks} marks -- expect a solid paragraph or two covering the key points in real depth, or a multi-step calculation / a short function to write or trace."
    if marks <= 7:
        return f"Worth {marks} marks -- expect a thorough, well-structured multi-paragraph answer covering multiple aspects, or a meatier coding/numerical problem."
    return (
        f"Worth {marks} marks -- expect a comprehensive answer. For a question this heavy, it's fine (and "
        "realistic) to ask for multiple sub-parts (e.g. a), b), c)) that together add up to the full mark "
        "value, covering the topic in real depth."
    )


def _custom_section_mix_counts(n, theory_percent):
    """
    Splits a section's n questions into (theory_n, practical_n) per the
    person's chosen theory/practical slider, rounding to the nearest whole
    question. "Practical" covers both numerical-calculation and
    coding-related questions combined -- the slider only controls theory
    vs. practical; the numerical/coding split within "practical" is still
    decided per-topic (see the prompt), same as before this option existed.
    """
    theory_n = round(n * theory_percent / 100)
    theory_n = max(0, min(n, theory_n))
    return theory_n, n - theory_n


def _generate_custom_section(qtype, marks, n, topics, difficulty, duration_label, theory_percent, or_count):
    """
    Generates one (type, marks) bucket of a custom paper -- e.g. "4 questions
    worth 2 marks each" -- AND its solutions in a single model call. See
    generate_custom_paper_and_solutions below for why: this used to be two
    full-paper calls made one after another (the whole paper's questions,
    then, once that came back, the whole paper's solutions) which is the
    slow part of custom paper generation. Splitting by bucket lets
    independent buckets run concurrently, and folding solutions into the
    same call as their questions means the model only has to think through
    each question once instead of two separate passes re-reading it.

    marks is None only for "mcq" (not broken down by marks on the form,
    worth a flat 1 mark each) or for a legacy bucket recovered from a
    pre-marks custom paper -- neither case reaches this function with
    or_count > 0 (OR alternatives are only ever distributed onto short/long
    buckets, which always carry a real marks value once generated fresh --
    see _distribute_or_counts).

    or_count: how many of this bucket's own `n` questions should get an OR
    alternative -- NOT a per-bucket "include or don't" flag. A real exam
    paper sprinkles OR choices on just a couple of questions, not every one,
    so the overall budget (1 or 2 short-answer questions, 1 or 2 long-answer
    questions, across the whole paper) is decided once up front in
    generate_custom_paper_and_solutions and split across whichever buckets
    of that type exist; this bucket only knows its own share of that split.
    0 means this bucket gets no OR alternatives at all.

    Returns (questions, solutions) as same-length, position-aligned lists.
    Whatever "id" the model used locally within this bucket is irrelevant
    and gets discarded -- the caller renumbers everything globally once all
    buckets are back, so a bucket doesn't need to know its own place in the
    final paper.
    """
    section_topics = topics[:] if topics else ["General"]
    random.shuffle(section_topics)
    topics_str = ", ".join(section_topics)
    label = _bucket_label(qtype, marks)
    theory_n, practical_n = _custom_section_mix_counts(n, theory_percent)

    if qtype == "mcq":
        format_instructions = 'Write 4 options labeled "A) ...", "B) ...", "C) ...", "D) ..." with exactly one correct answer. Each question is worth 1 mark.'
    else:
        format_instructions = _marks_depth_guidance(marks)

    or_count = max(0, min(n, or_count))
    if or_count > 0:
        if or_count >= n:
            or_scope = f"For EACH of these {n} questions, also write"
        else:
            or_scope = (
                f"For exactly {or_count} of these {n} questions -- your choice which ones, pick whichever suit an "
                f"alternate well -- also write"
            )
        or_block = f"""

OR ALTERNATIVES -- the person wants a few OR-alternative questions sprinkled in, like a standard exam paper where the student answers ONE of two options for a question (e.g. "Q5. Explain X. OR Explain Y.") on just some of the questions, not all of them. {or_scope} a genuine alternate version worth the same {marks} marks and matching difficulty/depth -- a different angle, sub-topic, or problem of similar weight, never a trivial reword of the same question. Put it in an "or_alternative" field on that question, shaped the same way as the question itself, and give it its own solution the same way in an "or_alternative" field on that question's solution entry. Leave "or_alternative" out entirely on every other question in this batch."""
        question_or_json = ',\n      "or_alternative": {"topic": "topic name for the alternative", "question": "the alternate question text", "code": "... (only if this alternative needs one)"} (only on the question(s) chosen for an OR alternative -- see OR ALTERNATIVES above)'
        solution_or_json = ',\n      "or_alternative": {"answer": "model answer for the alternate question", "code": "... (only if needed)"} (only to match a question that got one)'
    else:
        or_block = ""
        question_or_json = ""
        solution_or_json = ""

    # Shown as a literal, concrete example in the JSON template below rather
    # than buried in a comment inside the "answer" field's value -- some
    # providers key off the one worked example more closely than the text
    # instructions describing the mcq-specific shape, and a comment-only
    # mention was occasionally enough for a model to emit "answer" for mcq
    # solutions too (the shape every OTHER question type uses), leaving the
    # PDF with no correct_option to show. See history.py's
    # _resolve_mcq_answer for the matching defensive fallback on the reading
    # side, in case a model still deviates from this despite the fix here.
    if qtype == "mcq":
        solution_example_json = ',\n      "correct_option": "B",\n      "explanation": "why B is correct"'
        mcq_shape_reminder = (
            '\n\nIMPORTANT: for this "mcq" batch, every solution MUST use "correct_option" (just the letter -- '
            '"A", "B", "C", or "D") and "explanation" as two SEPARATE fields, exactly as shown above. Do NOT put '
            'the answer letter and explanation together into a single "answer" field -- that shape is for '
            '"short"/"long" solutions only, never mcq.'
        )
    else:
        solution_example_json = ',\n      "answer": "the full model answer"'
        mcq_shape_reminder = ""

    prompt = f"""Write {n} {label} for a custom practice paper, drawing on these topics: {topics_str}.

{format_instructions}

DIFFICULTY LEVEL -- write every question at this level: {_CUSTOM_DIFFICULTY_BRIEF[difficulty]}

TIME ALLOWED -- this paper is meant to be attempted in about {duration_label} in total. Pace the depth and length of these questions to fit that time realistically: a short time limit means more concise questions that can be answered quickly per mark, while a longer time limit allows more elaborate numericals and multi-part answers that expect a more thorough response.

QUESTION MIX -- the person generating this paper chose a {theory_percent}% theory / {100 - theory_percent}% practical split. Of these {n} questions, that means about {theory_n} should be pure theory/conceptual (explain, compare, define, discuss) and about {practical_n} should be practical. This is a deliberate choice they made on the form, so follow it closely rather than defaulting to mostly theory -- if practical_n is 0, don't sneak in a numerical or coding question anyway, and if theory_n is 0, don't fall back to easy theory recall just to pad the paper out.

A "practical" question is either a numeric-calculation question or a coding-related question (never both at once):
- Numeric: must require an actual calculation to reach a concrete answer -- give every value needed to compute it (e.g. work out a time/space complexity for a specific input size, count the operations/comparisons an algorithm performs for given input, a probability/combinatorics computation, a register/memory/address or bitwise arithmetic calculation, a circuit value). Never write a "numerical-looking" question that's actually just theory with a number mentioned in passing -- it must require the solver to compute something and arrive at a specific value.
- Coding: use a mix of these three distinct coding-question styles, and make "write code" the MOST common of the three -- it's the single most common coding-question format in real exams, and should not be crowded out by the other two:
  - WRITE CODE -- describe a task in plain words and ask the student to write a function/program/algorithm that accomplishes it, e.g. "Write a function to check whether a given string is a palindrome", "Write a program to find the GCD of two numbers using recursion", "Implement a stack using an array, with push, pop, and peek operations". Do NOT put a "code" field on a question like this -- there's no snippet to show, the student is the one writing the code. The actual working implementation belongs in that question's SOLUTION instead, which you'll also provide below.
  - TRACE / PREDICT OUTPUT -- show a snippet and ask what it prints, returns, or does. Put the snippet in the question's "code" field as real, properly indented, multi-line code (actual newline characters and consistent 4-space indentation between statements/braces, exactly as it would appear in an IDE -- never multiple statements crammed onto one line separated by semicolons). A multiple-choice question can absolutely be a "what does this output" question with the answer choices being candidate outputs -- that's a normal, common exam format, and one of the few coding styles that fits "mcq" well since it gives concrete options to choose from.
  - FIND AND FIX THE BUG -- show a snippet (same "code" field, same formatting rules) that has a deliberate bug, and ask the student to identify and/or fix it.
  Favor WRITE CODE especially if these are short-answer or long-answer questions. TRACE/PREDICT OUTPUT and FIND-THE-BUG are a natural fit for "mcq" (concrete answer options) but shouldn't be the only coding styles appearing.
Decide the numeric vs. coding split among the practical questions PER TOPIC, not uniformly: a topic like "OOP Concepts" leans coding, "Time Complexity" leans numerical and coding, "Probability" leans numerical, etc. Don't force a numerical or coding angle onto a topic that has no natural one -- pick whichever practical angle actually fits.

TOPIC COVERAGE -- a real exam paper is drawn FROM a syllabus, it doesn't necessarily touch every single topic on it. You do not need to use every topic listed above, and you do not need to split these {n} questions evenly across them. Pick whichever topics make the strongest, most natural set of questions -- it's fine to lean more heavily on some topics than others, or skip one entirely, as long as these questions are solid, well-rounded practice.

TOPIC ORDER -- shuffle which topic each question draws from across these {n} questions. Do NOT group every question of the same topic together in a block -- mix it up, the way topics are mixed up on a real exam paper.{or_block}

For EACH question, also give its solution in this same response -- you already have to work out the answer to write a good question, so provide it now rather than in a separate pass:
- For "mcq": the correct option letter (A, B, C, or D) and a short explanation of why it's correct.
- For "short"/"long": a complete model answer. If it required an actual numeric calculation, show the full step-by-step working that leads to the final answer, then state the final numeric answer clearly at the end -- never just the bare number with no working.
- If the question involved code (write-code, trace/predict-output, or find-and-fix-the-bug), include a "code" field on its solution holding real, properly indented, multi-line code: for WRITE CODE, the actual working implementation; for TRACE/PREDICT OUTPUT, the same code unchanged (with the answer/explanation walking through why that's the output); for FIND-AND-FIX, the corrected version. Omit "code" entirely on a solution for a question that didn't involve code.

Number these {n} questions sequentially starting at 1, and give each solution the same "id" as the question it answers.

Respond with this exact structure:
{{
  "questions": [
    {{
      "id": 1,
      "type": "{qtype}",
      "topic": "topic name",
      "question": "the question text"{question_or_json}
    }}
  ],
  "solutions": [
    {{
      "id": 1{solution_example_json}{solution_or_json}
    }}
  ]
}}{mcq_shape_reminder}

Only include the "options" field on a question for "mcq" type, "correct_option"/"explanation" on a solution for "mcq" type (use "answer" otherwise), and the "code" field (on a question and/or its solution, including inside "or_alternative") only when that specific question needs one, per the rules above."""

    response = generate_with_retry(prompt)
    data = parse_json_response(response)
    return data.get("questions", []), data.get("solutions", [])


def _distribute_or_counts(ordered_buckets):
    """
    Decides how many of the paper's short-answer questions, and how many of
    its long-answer questions, get an OR alternative -- a small number (1 or
    2 of each, like a real exam paper sprinkles a couple of "OR" choices in
    rather than offering one on every single question), never every question
    in those sections, and never mcq.

    The highest-mark bucket present in each type is always guaranteed one of
    those slots -- the heaviest question is the one a real exam paper is
    most likely to soften with an alternative, and the one a student most
    wants a choice on. A second slot, when the 1-vs-2 budget lands on 2, is
    picked at random across every question in that type (any bucket,
    including the highest-mark one again), so it's fine for both OR slots to
    land in the same bucket.

    Returns {(qtype, marks): or_count} with an entry for every short/long
    bucket in `ordered_buckets` (0 for a bucket that wasn't picked).
    """
    or_counts = {}
    for qtype in ("short", "long"):
        type_buckets = [(marks, n) for (bqtype, marks, n) in ordered_buckets if bqtype == qtype]
        if not type_buckets:
            continue
        total_n = sum(n for _, n in type_buckets)
        if total_n == 0:
            continue
        budget = random.randint(1, min(2, total_n))
        counts = {marks: 0 for marks, _ in type_buckets}

        # Guarantee the highest-mark bucket present gets the first slot --
        # treat a legacy marks=None bucket (no real mark value to compare)
        # as the lowest priority, never the "highest", by sorting None last.
        highest_marks, _ = max(type_buckets, key=lambda item: (item[0] is not None, item[0] or 0))
        counts[highest_marks] += 1
        budget -= 1

        if budget > 0:
            # Spread any remaining slot(s) randomly across every other
            # available question in this type -- the highest-mark bucket's
            # one already-used slot is excluded so the same question isn't
            # double counted, but that bucket can still win a second,
            # different question if the random pick lands on it again.
            slots = []
            for marks, n in type_buckets:
                available = n - (1 if marks == highest_marks else 0)
                slots.extend([marks] * max(0, available))
            random.shuffle(slots)
            for marks in slots[:budget]:
                counts[marks] += 1

        for marks, n in type_buckets:
            or_counts[(qtype, marks)] = min(counts[marks], n)
    return or_counts


def generate_custom_paper_and_solutions(topics, counts, difficulty="medium", duration_minutes=60, theory_percent=50, include_or=False):
    """
    topics: list of topic strings the person typed in.
    counts: nested dict like {"mcq": 5, "short": {"1": 2, "3": 4},
    "long": {"10": 1}} -- see _iter_custom_buckets for the exact shape this
    expects (and the older flat shape it also accepts).
    difficulty: "easy" | "medium" | "hard" -- defaults to "medium" for any
    other value.
    duration_minutes: how long the person wants the paper to take, purely
    informational/pacing -- printed on the cover like a real exam's "Time
    Allowed" and used to guide how much depth/length each question expects.
    theory_percent: 0-100, how much of the paper should be pure theory
    vs. "practical" (numerical-calculation and coding questions combined)
    -- defaults to 50/50, clamped to 0-100 for any out-of-range value.
    include_or: when True, a small number (1 or 2) of the paper's
    short-answer questions and a small number (1 or 2) of its long-answer
    questions -- never all of them, and never mcq -- each get a same-marks
    "OR" alternative the student can answer instead, like a standard exam
    paper. The heaviest short-answer question and the heaviest long-answer
    question are always among those picked -- see _distribute_or_counts and
    _generate_custom_section.

    Returns (paper_data, solutions_data) -- same shapes build_custom_paper_pdf
    already expects ({"questions": [...]}, {"solutions": [...]}), just built
    from per-bucket calls run concurrently instead of one generate_custom_paper
    call followed by one generate_custom_solutions call. That used to mean
    two full-paper, sequential model round-trips -- generate every question
    in the paper, wait for that to finish, then re-read every one of those
    questions from scratch to generate its solution. For a paper with a
    detailed difficulty/time/topic-balance prompt and full worked solutions
    (step-by-step numerical workings, real code for write-code/trace/debug
    questions), that's two slow calls back to back. Splitting by (type,
    marks) bucket -- none of which depend on each other -- and asking each
    bucket for its own solutions in the same call cuts that to one round of
    concurrent, smaller calls -- wall-clock time close to the slowest
    bucket instead of the sum of two full-paper passes, with nothing
    dropped from what gets asked for. Each question comes back carrying its
    own "marks" field (1 for mcq, whatever the bucket's mark value was for
    short/long) for build_custom_paper_pdf to show and total up.
    """
    if difficulty not in _CUSTOM_DIFFICULTIES:
        difficulty = "medium"
    duration_label = _duration_label(duration_minutes)
    try:
        theory_percent = int(theory_percent)
    except (TypeError, ValueError):
        theory_percent = 50
    theory_percent = max(0, min(100, theory_percent))
    include_or = bool(include_or)
    ordered_buckets = list(_iter_custom_buckets(counts))
    or_counts_by_bucket = _distribute_or_counts(ordered_buckets) if include_or else {}

    if DRY_RUN:
        questions = []
        solutions = []
        qid = 1
        for qtype, marks, n in ordered_buckets:
            effective_marks = marks if marks is not None else 1
            topic_seq = _shuffled_topic_sequence(topics, n)
            bucket_or_count = or_counts_by_bucket.get((qtype, marks), 0)
            or_positions = set(random.sample(range(n), bucket_or_count)) if bucket_or_count else set()
            for i in range(n):
                topic = topic_seq[i]
                q = {
                    "id": qid,
                    "type": qtype,
                    "topic": topic,
                    "marks": effective_marks,
                    "question": f"[DRY RUN] Placeholder {difficulty} {qtype} question #{qid} about {topic} ({effective_marks} marks).",
                }
                if qtype == "mcq":
                    q["options"] = ["A) [DRY RUN] Option A", "B) [DRY RUN] Option B", "C) [DRY RUN] Option C", "D) [DRY RUN] Option D"]
                if qid % 3 == 0:
                    q["code"] = f"# [DRY RUN] placeholder snippet for question {qid}\nprint('hello')"
                if i in or_positions:
                    q["or_alternative"] = {"topic": topic, "question": f"[DRY RUN] Placeholder OR-alternative for question {qid}."}
                questions.append(q)

                if qtype == "mcq":
                    sol = {"id": qid, "type": "mcq", "correct_option": "A", "explanation": f"[DRY RUN] Placeholder explanation for question {qid}."}
                else:
                    sol = {"id": qid, "type": qtype, "answer": f"[DRY RUN] Placeholder model answer for question {qid}."}
                if q.get("code"):
                    sol["code"] = "# [DRY RUN] placeholder solution code\nprint('hello')"
                if q.get("or_alternative"):
                    sol["or_alternative"] = {"answer": f"[DRY RUN] Placeholder OR-alternative answer for question {qid}."}
                solutions.append(sol)
                qid += 1
        return {"questions": questions}, {"solutions": solutions}

    section_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(ordered_buckets), _MAX_CONCURRENT_BUCKET_CALLS))) as pool:
        future_to_key = {
            pool.submit(
                _generate_custom_section, qtype, marks, n, topics, difficulty, duration_label, theory_percent,
                or_counts_by_bucket.get((qtype, marks), 0),
            ): (qtype, marks)
            for qtype, marks, n in ordered_buckets
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            # .result() re-raises whatever that bucket's call raised
            # (QuotaExceededError/ModelOverloadedError/NetworkError) --
            # letting it propagate here is fine, the `with` block still
            # waits for every other in-flight bucket to finish before this
            # exception actually leaves the function, so nothing is left
            # running in the background.
            section_results[key] = future.result()

    questions = []
    solutions = []
    qid = 1
    for qtype, marks, n in ordered_buckets:
        section_questions, section_solutions = section_results[(qtype, marks)]
        effective_marks = marks if marks is not None else 1
        # Pair up by position, not the model's own locally-scoped "id" --
        # robust to a bucket mis-numbering or skipping one, and lets every
        # bucket restart its own numbering at 1 with no risk of collision.
        for i, q in enumerate(section_questions):
            q = dict(q)
            q["id"] = qid
            q["type"] = qtype
            q["marks"] = effective_marks
            questions.append(q)

            s = dict(section_solutions[i]) if i < len(section_solutions) else {}
            s["id"] = qid
            solutions.append(s)
            qid += 1

    return {"questions": questions}, {"solutions": solutions}


def generate_custom_notes(topics):
    """
    topics: list of topic strings the person typed in (Custom Notes, Notes
    tab). Unlike generate_topic_notes (always explanation/key_patterns/
    code_skeleton for every topic), this asks the AI to decide, per topic,
    which of definition/explanation/examples/syntax/mnemonics/table/
    flowchart are actually useful and to leave the rest out -- a topic like
    "Normal Distribution" might get a table + flowchart but no syntax,
    while "8086 Addressing Modes" might get syntax but no table.
    """
    if DRY_RUN:
        return {
            "notes": [
                {
                    "topic": topic,
                    "definition": f"[DRY RUN] Placeholder definition for {topic}.",
                    "explanation": f"[DRY RUN] Placeholder explanation for {topic}.",
                    "examples": [f"[DRY RUN] Example 1 for {topic}", f"[DRY RUN] Example 2 for {topic}"],
                    "syntax": f"# [DRY RUN] placeholder syntax for {topic}",
                    "mnemonics": [f"[DRY RUN] mnemonic for {topic}"],
                    "table": {
                        "headers": ["[DRY RUN] Col A", "[DRY RUN] Col B"],
                        "rows": [["[DRY RUN] a1", "[DRY RUN] b1"], ["[DRY RUN] a2", "[DRY RUN] b2"]],
                    },
                    "flowchart": [f"[DRY RUN] Step 1 for {topic}", f"[DRY RUN] Step 2 for {topic}", f"[DRY RUN] Step 3 for {topic}"],
                }
                for topic in topics
            ]
        }

    topics_str = ", ".join(topics)
    prompt = f"""Write study notes for someone learning these topics, for easier learning and quick revision: {topics_str}.

These topics may come from ANY subject (programming, math, probability, electronics, microprocessors, networking, etc.), not just coding -- tailor what you include to what's actually useful for each specific topic.

For each topic, decide for yourself which of the following elements genuinely help someone learn or revise it, and ONLY include those elements. Do not force a field in just to fill it out -- omit a field entirely (don't include the key at all) if it wouldn't add value for that particular topic.

Possible elements:
- "definition": a precise, short definition of the topic (1-3 sentences). Include this for almost every topic.
- "explanation": a clear plain-language explanation of the concept, how it works, and why it matters. Include this for almost every topic.
- "examples": a list of short concrete examples or use cases. Include when examples would clarify the concept. IMPORTANT: if an example IS a code snippet with more than one statement (e.g. a class with a method, a loop with a body, several related lines), you MUST write it as properly formatted, multi-line, indented code -- real newline characters between statements/lines and consistent indentation (4 spaces per nesting level), exactly as it would appear in an IDE. Never cram multiple statements, or a whole class/function definition, onto one line separated by semicolons. For instance, write
"class Shape {{\n    virtual void draw();\n}};\n\nclass Circle : public Shape {{\n    void draw();\n}};\n\nShape* s = new Circle();\ns->draw();"
NOT
"class Shape {{ virtual void draw(); }}; class Circle : public Shape {{ void draw(); }}; Shape* s = new Circle(); s->draw();"
A short example that's genuinely a single expression or phrase (e.g. "arr.sort() sorts in place") doesn't need this -- this only applies when the example itself is multi-statement code.
- "syntax": a short code/notation/formula block showing the general syntax or structure (e.g. a programming construct, a math formula, an assembly instruction format). Only include when the topic actually has a notable syntax/notation worth showing. Same rule as above: multi-line code must use real newlines and indentation, never one semicolon-chained line.
- "mnemonics": a list of short memory aids or rules of thumb. Only include when a genuinely useful mnemonic exists for this topic.
- "table": a comparison/reference table as {{"headers": [...], "rows": [[...], ...]}}. Only include when the topic naturally involves comparing multiple things (e.g. register sizes, complexity classes, distribution properties).
- "flowchart": an ordered list of short step strings describing a process or sequence (e.g. an algorithm's steps, how to solve a type of problem). Only include when the topic is fundamentally a process/procedure.

IMPORTANT -- symbols: if you use ANY mathematical, logical, or notational symbol anywhere in the notes (e.g. Greek letters like Ω, Θ, Σ, π, Δ; operators like ∈, ∀, ∃, ≈, ∝; or any other non-obvious notation), you MUST explain what that symbol means and how to read it out loud the first time it appears -- either inline right after the symbol (e.g. "Θ(n) (read 'Theta of n', meaning a tight bound on growth rate)") or as part of the definition/explanation text. Never use a symbol without naming it and defining it in plain words somewhere in the note. Do not assume the reader already knows standard notation.

Respond with this exact structure:
{{
  "notes": [
    {{
      "topic": "topic name",
      "definition": "...",
      "explanation": "...",
      "examples": ["...", "..."],
      "syntax": "...",
      "mnemonics": ["..."],
      "table": {{"headers": ["...", "..."], "rows": [["...", "..."]]}},
      "flowchart": ["step 1", "step 2"]
    }}
  ]
}}

Remember: omit any of definition/explanation/examples/syntax/mnemonics/table/flowchart entirely for a topic when it wouldn't genuinely help -- don't pad every topic with all seven fields. Also remember: every symbol you use must be explained in plain words somewhere in the note."""
    response = generate_with_retry(prompt)
    return parse_json_response(response)


def generate_topic_notes(weak_topics):
    if DRY_RUN:
        return {
            "notes": [
                {
                    "topic": topic,
                    "explanation": f"[DRY RUN] Placeholder explanation for {topic}.",
                    "key_patterns": ["[DRY RUN] pattern 1", "[DRY RUN] pattern 2"],
                    "code_skeleton": "# [DRY RUN] placeholder skeleton",
                }
                for topic in weak_topics
            ]
        }
    topics_str = ", ".join(weak_topics)
    prompt = f"""Write short conceptual study notes for a programmer preparing to practice these topics: {topics_str}.

For each topic:
- explain the core idea/pattern in plain language (as if teaching someone who understands basic programming but hasn't internalized this specific technique yet)
- list 2-3 key patterns or signals that tell you "this is a {{topic}} problem"
- include a short generic Python code skeleton (5-10 lines) showing the general reusable SHAPE of this pattern (e.g. for two-pointer: the initial pointer setup and the while loop condition). This must NOT be a solved problem or a complete working solution — just the structural template someone would adapt.

Respond with this exact structure:
{{
  "notes": [
    {{
      "topic": "topic name",
      "explanation": "plain language explanation of the core concept",
      "key_patterns": ["pattern or signal 1", "pattern or signal 2"],
      "code_skeleton": "python code as a single string, showing the general reusable pattern, not a solved problem"
    }}
  ]
}}"""
    response = generate_with_retry(prompt)
    return parse_json_response(response)


def get_calendar_service(user_id):
    """Builds a Calendar API client using THIS user's own stored credentials
    (see auth.save_calendar_credentials, written during their Google
    sign-in). Raises CalendarNotConnectedError -- never crashes the caller
    outright -- if they have none, or if refreshing an expired token fails,
    so every caller can decide for itself how to degrade gracefully."""
    creds_json = auth.get_calendar_credentials(user_id)
    if not creds_json:
        raise CalendarNotConnectedError(
            "Calendar isn't connected for this account yet. Sign out and "
            "sign back in to grant calendar access."
        )

    creds = Credentials.from_authorized_user_info(json.loads(creds_json), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                raise CalendarNotConnectedError(
                    "Your calendar connection expired. Sign out and sign "
                    "back in to reconnect it."
                )
            # Save the refreshed token back so we don't have to do this
            # again every time until it next expires.
            auth.save_calendar_credentials(user_id, creds.to_json())
        else:
            raise CalendarNotConnectedError(
                "Calendar isn't connected for this account yet. Sign out "
                "and sign back in to grant calendar access."
            )

    return build('calendar', 'v3', credentials=creds)


def fetch_calendar_events(deadline, user_id):
    service = get_calendar_service(user_id)
    time_min = datetime.now(timezone.utc).isoformat()
    time_max = f"{deadline.isoformat()}T23:59:59Z"
    events_result = service.events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    return events_result.get('items', [])
DEADLINE_KEYWORDS = ["exam", "deadline", "due", "test", "viva", "assignment", "submission", "quiz", "final", "midterm"]


def fetch_calendar_candidates(user_id, days_ahead=90):
    service = get_calendar_service(user_id)
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()
    events_result = service.events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    events = events_result.get('items', [])

    candidates = []
    for event in events:
        summary = event.get("summary", "")
        summary_lower = summary.lower()
        if any(keyword in summary_lower for keyword in DEADLINE_KEYWORDS):
            start = event["start"]
            if "dateTime" in start:
                event_date = datetime.fromisoformat(start["dateTime"]).date()
            else:
                event_date = date.fromisoformat(start["date"])
            candidates.append({
                "title": summary,
                "date": event_date.isoformat(),
            })
    return candidates

def build_day_skeleton(deadline, user_id):
    """Returns (skeleton, calendar_note). calendar_note is None when the
    calendar was read successfully; otherwise it's a human-readable message
    explaining why every day was treated as free, so the caller can surface
    it instead of the plan silently ignoring a missing calendar connection."""
    today = date.today()
    total_days = (deadline - today).days

    busy_dates = set()
    calendar_note = None
    try:
        events = fetch_calendar_events(deadline, user_id)
        for event in events:
            start = event["start"]
            if "dateTime" in start:
                event_date = datetime.fromisoformat(start["dateTime"]).date()
                busy_dates.add(event_date)
    except CalendarNotConnectedError as e:
        calendar_note = str(e)

    skeleton = []
    for i in range(total_days + 1):
        day = today + timedelta(days=i)
        skeleton.append({
            "date": day.isoformat(),
            "status": "busy" if day in busy_dates else "free",
        })
    return skeleton, calendar_note


def assign_topics_to_days(skeleton, topics):
    free_days = [d for d in skeleton if d["status"] == "free"]
    for i, day in enumerate(free_days):
        day["topic"] = topics[i % len(topics)]
    return skeleton


def generate_deadline_plan_content(skeleton):
    free_days = [d for d in skeleton if d["status"] == "free"]

    if DRY_RUN:
        return {
            "plan": [
                {
                    "date": d["date"],
                    "topic": d["topic"],
                    "task": f"[DRY RUN] Placeholder task for {d['topic']}.",
                }
                for d in free_days
            ]
        }

    days_str = "\n".join(f"{d['date']}: focus on {d['topic']}" for d in free_days)
    prompt = f"""You are helping a programmer prepare for a deadline. Below is a day-by-day schedule of free days and the topic assigned to each day.

{days_str}

For each date listed, write one specific, realistic practice task for that day (e.g. "solve 2 problems using sliding window, focusing on variable-size windows"). Keep each task doable in a single session.

Respond with this exact structure:
{{
  "plan": [
    {{
      "date": "YYYY-MM-DD",
      "topic": "topic name",
      "task": "specific task for that day"
    }}
  ]
}}"""
    response = generate_with_retry(prompt)
    return parse_json_response(response)


def print_practice_paper(paper):
    print("\n=== YOUR PRACTICE PAPER ===\n")
    for p in paper["problems"]:
        print(f"Problem {p['id']}: {p['title']}  [{p['topic']} - {p['difficulty']}]")
        print(p["statement"])
        print()


def print_solutions(solutions):
    print("\n=== SOLUTIONS ===\n")
    for s in solutions["solutions"]:
        print(f"--- Solution {s['id']} ---")
        print(f"Approach: {s['approach']}\n")
        print(s["code"])
        print(f"\nComplexity: {s['complexity']}\n")


def print_topic_notes(notes):
    print("\n=== TOPIC NOTES ===\n")
    for n in notes["notes"]:
        print(f"--- {n['topic']} ---")
        print(n["explanation"])
        print("Look for:")
        for p in n["key_patterns"]:
            print(f"  - {p}")
        print("\nGeneral pattern:")
        print(n["code_skeleton"])
        print()


def print_deadline_plan(skeleton, plan_data):
    print("\n=== YOUR PRACTICE PLAN ===\n")
    plan_by_date = {p["date"]: p for p in plan_data["plan"]}
    for day in skeleton:
        if day["status"] == "busy":
            print(f"{day['date']}: (busy - you have a commitment that day, no practice planned)")
        else:
            p = plan_by_date.get(day["date"])
            if p:
                print(f"{day['date']} [{p['topic']}]: {p['task']}")
    print()


def run_deadline_plan():
    deadline_str = input("Enter your deadline (YYYY-MM-DD): ").strip()
    deadline = date.fromisoformat(deadline_str)

    if deadline < date.today():
        print("That deadline is in the past. Skipping the plan.")
        return

    topics_str = input("Enter topics to focus on, comma separated: ").strip()
    topics = [t.strip() for t in topics_str.split(",") if t.strip()]

    if not topics:
        print("No topics entered. Skipping the plan.")
        return

    print("Checking your calendar for free days...")
    skeleton = build_day_skeleton(deadline)

    if not any(d["status"] == "free" for d in skeleton):
        print("No free days found before your deadline.")
        return

    assign_topics_to_days(skeleton, topics)

    print("Generating your practice plan...")
    plan_data = generate_deadline_plan_content(skeleton)
    print_deadline_plan(skeleton, plan_data)


def run():
    if DRY_RUN:
        print("[DRY RUN MODE — no Gemini API calls will be made]\n")

    print(f"[{datetime.now().isoformat()}] Fetching LeetCode data...")
    topics = fetch_leetcode_data(username)
    save_to_tracker(topics)
    print(f"Saved {len(topics)} topics to tracker.db")

    stored_topics = get_topics_from_tracker()
    weak_topics = get_weak_topics(stored_topics, n=3)
    print(f"Weakest topics identified: {', '.join(weak_topics)}")

    print("Generating topic notes...")
    notes = generate_topic_notes(weak_topics)
    print_topic_notes(notes)

    input("Press Enter when you're ready for the practice paper...")

    print("Generating your practice paper...")
    paper = generate_practice_paper(weak_topics)
    print_practice_paper(paper)

    input("Press Enter when you're ready to see the solutions...")

    print("Generating solutions...")
    solutions = generate_solutions(paper)
    print_solutions(solutions)

    make_plan = input("\nWould you like a deadline-based practice plan too? (y/n): ").strip().lower()
    if make_plan == "y":
        run_deadline_plan()


if __name__ == "__main__":
    run()