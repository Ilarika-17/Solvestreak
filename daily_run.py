"""
Fully automated daily Solvestreak run — no interactive input, no browser.

This does the same work as clicking through Dashboard -> Notes -> Practice Paper
-> Solutions in the Flask app once, but unattended: for every signed-in account
that has a LeetCode username set in Settings, it fetches that person's LeetCode
stats, picks their weakest topics, generates notes/paper/solutions with Gemini,
saves the session to history.db under their account, and drops a PDF of the
run into daily_pdfs/ so there's something to show for it even if they never
open the app that day.

Run it by hand any time with:
    python daily_run.py

To have Windows run it automatically every day, use schedule_task.py (see
that file for usage), or set the time from the app's own Settings page --
both just point the same Windows Scheduled Task at this script.

Progress and any errors are appended to logs/daily_run_YYYY-MM-DD.log, since
nobody is watching a terminal when this fires at 6am. One account's failure
(quota, network, etc.) is logged and skipped rather than stopping the run for
everyone else.
"""
import os

# Make every relative path (tracker.db, history.db, users.db, .env, logs/,
# daily_pdfs/) resolve against THIS file's folder, no matter how/where this
# gets launched from (Task Scheduler's working directory is not this project
# folder).
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import traceback
from datetime import datetime

import solvestreak as ss
import history
import auth

LOG_DIR = "logs"
PDF_DIR = "daily_pdfs"
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"daily_run_{datetime.now().strftime('%Y-%m-%d')}.log")


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_for_user(user):
    label = user["name"] or user["email"] or f"user #{user['id']}"
    log(f"--- {label} ({user['leetcode_username']}) ---")
    try:
        log("Fetching LeetCode data...")
        topics = ss.fetch_leetcode_data(user["leetcode_username"])
        ss.save_to_tracker(topics, user["id"])
        stored_topics = ss.get_topics_from_tracker(user["id"])
        weak_topics = ss.get_weak_topics(stored_topics, n=3)
        log(f"Weakest topics: {', '.join(weak_topics)}")

        run_id = history.start_run(weak_topics, user["id"])
        log(f"History run #{run_id} started.")

        log("Generating topic notes...")
        notes = ss.generate_topic_notes(weak_topics)
        history.update_run(run_id, notes=notes)

        log("Generating practice paper...")
        paper = ss.generate_practice_paper(weak_topics)
        history.update_run(run_id, paper=paper)

        log("Generating solutions...")
        solutions = ss.generate_solutions(paper)
        history.update_run(run_id, solutions=solutions)

        log("Building today's PDF...")
        run_data = history.get_run(run_id, user["id"])
        pdf_bytes = history.build_pdf(run_data)
        safe_label = "".join(c if c.isalnum() else "_" for c in label).strip("_") or f"user_{user['id']}"
        pdf_path = os.path.join(
            PDF_DIR, f"solvestreak_{datetime.now().strftime('%Y-%m-%d')}_{safe_label}_run_{run_id}.pdf"
        )
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        log(f"Done for {label}. Run #{run_id} saved. PDF saved to {pdf_path}")
    except ss.QuotaExceededError as e:
        log(f"STOPPED for {label}: Gemini daily quota exhausted — {e}")
    except ss.ModelOverloadedError as e:
        log(f"STOPPED for {label}: Gemini overloaded even after retries — {e}")
    except ss.NetworkError as e:
        log(f"STOPPED for {label}: network error reaching Gemini — {e}")
    except Exception:
        log(f"STOPPED for {label}: unexpected error:")
        log(traceback.format_exc())


def main():
    log("Starting daily Solvestreak run...")
    history.init_history_db()
    auth.init_db()

    users = [u for u in auth.get_all_users() if u["leetcode_username"]]
    if not users:
        log("No accounts have a LeetCode username set yet (add one in Settings) -- nothing to do.")
        return

    log(f"Running for {len(users)} account(s): {', '.join(u['leetcode_username'] for u in users)}")
    for user in users:
        run_for_user(user)

    log("Daily run finished for all accounts.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("STOPPED: unexpected top-level error:")
        log(traceback.format_exc())
