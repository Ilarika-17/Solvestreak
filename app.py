import io
import os
import secrets
import traceback
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from dotenv import load_dotenv

import solvestreak as ss
import history
import auth
import scheduler
import feedback
import usage
import streaks

load_dotenv()

# How many AI generations (Practice Paper, Notes, Solutions, Custom Paper,
# Custom Notes -- each counts as 1, regardless of type) one person can use
# per day. Protects the shared Groq/Gemini/OpenRouter daily quota from being
# used up by a single person once real users are on the app. Configurable
# via .env so it can be tuned without a code change as real usage patterns
# become clear.
DAILY_GENERATIONS_PER_USER = int(os.environ.get("DAILY_GENERATIONS_PER_USER", "3"))

# How many real provider calls (not "generations" -- a 9-bucket Custom Paper
# is 9 calls) the WHOLE app can make across every user, per day, before new
# generations pause for everyone until the next day. Deliberately kept below
# Groq's documented ~1,000/day free-tier ceiling (the provider essentially
# all traffic hits first in the normal case) so the app fails gracefully
# with a clear message instead of random users hitting raw provider errors
# once the real ceiling is reached.
DAILY_GLOBAL_CALL_BUDGET = int(os.environ.get("DAILY_GLOBAL_CALL_BUDGET", "750"))


def _log_unexpected_error(stage, exc):
    """
    Writes a timestamped traceback to error.log in the project directory for
    any exception a route didn't specifically anticipate (QuotaExceededError/
    ModelOverloadedError/NetworkError already get their own clear message and
    status code -- this is the catch-all for everything else, e.g. a bug in
    PDF rendering or a provider returning a shape we didn't expect). Without
    this, an unhandled exception here just shows the person a generic
    "something went wrong" with the real cause visible only in whatever
    terminal the server happens to be running in -- easy to miss, and gone
    once that terminal is closed. Never raises itself -- a logging failure
    shouldn't turn into a second error on top of the first.
    """
    try:
        with open("error.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat()} [{stage}] ---\n")
            f.write(traceback.format_exc())
    except Exception:
        pass

app = Flask(__name__)
history.init_history_db()
auth.init_db()
feedback.init_feedback_db()
usage.init_usage_db()

# Starts the daily-run schedule immediately at app startup (not just once
# someone opens Settings and re-saves a time). On Windows this is a no-op
# -- schtasks already runs independently of this process -- but once
# deployed to a Linux server this is what kicks off the in-process
# APScheduler job from whatever time was last saved. See scheduler.py.
scheduler.start()

# A stable secret key is required so that signed session cookies stay valid
# across app restarts -- if this changed every time the app started, every
# signed-in user would be logged out whenever the server restarted. It's
# generated once (see auth.py's .env note) and read from .env from then on.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# How long someone stays signed in without touching the app. This is what
# makes "don't ask me to sign in again until I log out" actually work, even
# if the computer is restarted or the app isn't opened again for a while.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=400)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

app.register_blueprint(auth.bp)


@app.context_processor
def inject_current_user():
    user = auth.current_user()
    # is_admin is exposed here (rather than computed inline in base.html)
    # so the template never has to know the admin's email or import auth
    # itself -- it just checks a plain boolean, same as current_user.
    # user_streak is injected globally (not just on index()) so the sidebar
    # can show a small streak badge on every page, not only the dashboard.
    user_streak = streaks.get_streak(user["id"]) if user else None
    return {"current_user": user, "is_admin": auth.is_admin(user), "user_streak": user_streak}


# Per-user in-memory cache. Keyed by user id so two different signed-in
# people never see each other's cached weak topics / notes / paper /
# solutions / in-progress run id. This only caches the current session's
# scratch work -- the durable, cross-restart data lives in history.db and
# tracker.db, both now keyed by user_id too.
state = defaultdict(dict)


def get_state():
    user = auth.current_user()
    return state[user["id"]]


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.errorhandler(ss.QuotaExceededError)
def handle_quota_exceeded(error):
    return render_template(
        "error.html",
        title="Daily AI quota reached",
        message=str(error),
        back_href="/",
        back_label="Back to Dashboard",
    ), 429


@app.errorhandler(ss.ModelOverloadedError)
def handle_model_overloaded(error):
    return render_template(
        "error.html",
        title="Gemini is busy right now",
        message=str(error),
        back_href="/",
        back_label="Back to Dashboard",
    ), 503


@app.errorhandler(ss.NetworkError)
def handle_network_error(error):
    return render_template(
        "error.html",
        title="Couldn't reach the AI service",
        message=str(error),
        back_href="/",
        back_label="Back to Dashboard",
    ), 503


def _check_usage_budget(user, call_count=1):
    """
    Call this right before making any real AI provider call, with
    call_count set to how many provider calls that generation is about to
    make (1 for most actions, up to 9 for a multi-bucket Custom Paper).
    Raises ss.QuotaExceededError -- the same error the app already shows a
    friendly "Daily AI quota reached" page/JSON error for everywhere else --
    if either this person's own daily allowance or the app-wide daily safety
    budget would be exceeded, so the request is stopped BEFORE it spends a
    real provider call.

    The admin account is exempt from its own per-user allowance (so testing
    the app never gets in the admin's way -- see auth.is_admin), but never
    from the global budget: nothing can call a provider that's genuinely out
    of quota for the day, admin included.
    """
    if not auth.is_admin(user) and usage.get_user_count(user["id"]) >= DAILY_GENERATIONS_PER_USER:
        raise ss.QuotaExceededError(
            f"You've used all {DAILY_GENERATIONS_PER_USER} of your free generations for today. "
            "Your limit resets at midnight -- thanks for your patience!"
        )
    if usage.get_global_count() + call_count > DAILY_GLOBAL_CALL_BUDGET:
        raise ss.QuotaExceededError(
            "Solvestreak is seeing unusually high demand right now and has paused new generations "
            "to protect everyone's daily AI quota. Please try again tomorrow."
        )


def get_weak_topics_cached(refresh=False):
    user = auth.current_user()
    user_state = get_state()

    leetcode_username = user["leetcode_username"]
    if not leetcode_username:
        # Nobody's set a LeetCode profile for this account yet -- don't
        # crash, just show the normal "nothing to show yet" empty state
        # until they add one in Settings.
        user_state["weak_topics"] = []
        user_state["topics_tracked"] = 0
        user_state["tracked_topics"] = []
        return user_state["weak_topics"]

    if refresh or "weak_topics" not in user_state:
        topics = ss.fetch_leetcode_data(leetcode_username)
        ss.save_to_tracker(topics, user["id"])
        stored_topics = ss.get_topics_from_tracker(user["id"])
        raw_weakest = ss.get_weak_topics(stored_topics, n=3)
        missed = history.get_recently_missed_topics(user["id"], limit=3)
        user_state["weak_topics"] = _blend_weak_topics(missed, raw_weakest, n=3)
        # The total number of distinct LeetCode topics this account has any
        # solve data for (not just the weak ones picked above) -- shown as
        # the "Topics Tracked" stat on the dashboard.
        user_state["topics_tracked"] = len(stored_topics)
        # The full (tag_name, problems_solved) list, most-solved first --
        # shown in the "Topics Tracked" detail popup on the dashboard so a
        # user can see every topic behind that count, not just the number.
        user_state["tracked_topics"] = sorted(stored_topics, key=lambda t: t[1], reverse=True)
    return user_state["weak_topics"]


def _blend_weak_topics(missed_topics, raw_weakest, n=3):
    """
    Closes the loop between what a user says they got wrong (the
    Correct/Incorrect marks on the Solutions page) and what gets picked as
    their next weak topics, instead of relying purely on raw LeetCode
    solve counts. Self-reported missed topics are prioritized first (most
    recently missed first), then backfilled with the raw LeetCode-weakest
    topics to fill out to n -- so a topic someone is actively struggling
    with keeps resurfacing even if their LeetCode solve count for it looks
    fine.
    """
    blended = []
    for topic in missed_topics + raw_weakest:
        if topic not in blended:
            blended.append(topic)
        if len(blended) >= n:
            break
    return blended[:n]


def get_current_run_id():
    user_state = get_state()
    if "current_run_id" not in user_state:
        user = auth.current_user()
        user_state["current_run_id"] = history.start_run(
            user_state.get("weak_topics", []), user["id"]
        )
    return user_state["current_run_id"]


def get_notes_cached():
    user_state = get_state()
    if "notes" not in user_state:
        user = auth.current_user()
        _check_usage_budget(user)
        weak_topics = get_weak_topics_cached()
        user_state["notes"] = ss.generate_topic_notes(weak_topics)
        usage.record_usage(user["id"], 1)
        history.update_run(get_current_run_id(), notes=user_state["notes"])
    return user_state["notes"]


def get_paper_cached():
    user_state = get_state()
    if "paper" not in user_state:
        user = auth.current_user()
        _check_usage_budget(user)
        weak_topics = get_weak_topics_cached()
        user_state["paper"] = ss.generate_practice_paper(weak_topics)
        usage.record_usage(user["id"], 1)
        history.update_run(get_current_run_id(), paper=user_state["paper"])
    return user_state["paper"]


def get_solutions_cached():
    user_state = get_state()
    if "solutions" not in user_state:
        # get_paper_cached() above already runs its own usage check/record
        # for the paper itself (it may or may not actually generate
        # anything, depending on whether the paper was already cached) --
        # this is a second, separate check/record for the solutions call,
        # which is its own distinct provider call either way.
        paper = get_paper_cached()
        user = auth.current_user()
        _check_usage_budget(user)
        user_state["solutions"] = ss.generate_solutions(paper)
        usage.record_usage(user["id"], 1)
        history.update_run(get_current_run_id(), solutions=user_state["solutions"])
    return user_state["solutions"]


@app.route("/")
@auth.login_required
def index():
    user = auth.current_user()
    weak_topics = get_weak_topics_cached(refresh=True)
    return render_template(
        "index.html",
        weak_topics=weak_topics,
        active_step="topics",
        leetcode_username_missing=not user["leetcode_username"],
        topics_tracked=get_state().get("topics_tracked", 0),
        tracked_topics=get_state().get("tracked_topics", []),
        today_generations=usage.get_user_count(user["id"]),
        daily_generations_per_user=DAILY_GENERATIONS_PER_USER,
        is_admin_unlimited=auth.is_admin(user),
        weak_topic_frequency=history.get_weak_topic_frequency(user["id"], limit=5),
        best_mock_test=history.get_best_mock_test_attempt(user["id"]),
    )


@app.route("/notes", methods=["GET", "POST"])
@auth.login_required
def notes():
    notes_data = get_notes_cached()
    user = auth.current_user()
    return render_template(
        "notes.html",
        notes=notes_data,
        active_step="notes",
        run_id=get_state().get("current_run_id"),
        note_folders=history.get_note_folders(user["id"]),
    )


@app.route("/paper", methods=["GET", "POST"])
@auth.login_required
def paper():
    paper_data = get_paper_cached()
    return render_template(
        "paper.html", paper=paper_data, active_step="paper", run_id=get_state().get("current_run_id")
    )


@app.route("/solutions", methods=["GET", "POST"])
@auth.login_required
def solutions():
    solutions_data = get_solutions_cached()
    user = auth.current_user()
    run_id = get_state().get("current_run_id")
    paper_data = get_state().get("paper") or {}
    topic_by_id = {p["id"]: p.get("topic") for p in paper_data.get("problems", [])}
    prior_grades = history.get_question_results_for_run(run_id, user["id"]) if run_id else {}
    return render_template(
        "solutions.html",
        solutions=solutions_data,
        active_step="solutions",
        run_id=run_id,
        topic_by_id=topic_by_id,
        prior_grades=prior_grades,
    )


@app.route("/paper/mock-test/finish", methods=["POST"])
@auth.login_required
def finish_mock_test():
    """
    Records one timed mock-test attempt (see the Start Timed Mock Test /
    Finish Test flow on paper.html + its app.js timer). The timer itself
    runs entirely client-side -- this is only called once, when the user
    clicks Finish, with the elapsed time the client already computed from
    its own start timestamp. Trusting the client's elapsed time here is a
    deliberate tradeoff: this is a personal practice stat, not something
    that gates access or a shared leaderboard, so there's nothing to gain
    by a user lying to themselves about their own time.
    """
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    duration_minutes = data.get("duration_minutes")
    elapsed_seconds = data.get("elapsed_seconds")

    if run_id is None or duration_minutes is None or elapsed_seconds is None:
        return {"error": "Missing run_id, duration_minutes, or elapsed_seconds."}, 400
    if history.get_run(run_id, user["id"]) is None:
        return {"error": "Run not found."}, 404

    try:
        duration_minutes = int(duration_minutes)
        elapsed_seconds = int(elapsed_seconds)
    except (TypeError, ValueError):
        return {"error": "duration_minutes and elapsed_seconds must be numbers."}, 400
    if elapsed_seconds < 0 or duration_minutes <= 0:
        return {"error": "Invalid duration/elapsed time."}, 400

    within_time_limit = elapsed_seconds <= duration_minutes * 60
    history.record_mock_test_attempt(user["id"], run_id, duration_minutes, elapsed_seconds, within_time_limit)
    return {"ok": True, "within_time_limit": within_time_limit}


@app.route("/solutions/grade", methods=["POST"])
@auth.login_required
def grade_solution():
    """
    Mark one practice-paper question correct/incorrect (see the
    Correct/Incorrect buttons on solutions.html). This is what feeds
    get_recently_missed_topics / _blend_weak_topics above -- the actual
    "closing the loop" step, separate from raw LeetCode solve counts.
    Called via fetch() from app.js, not a full page submit, so marking a
    question doesn't reload the whole Solutions page.
    """
    user = auth.current_user()
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    problem_id = data.get("problem_id")
    topic = data.get("topic")
    correct = data.get("correct")

    if run_id is None or problem_id is None or correct is None:
        return {"ok": False, "error": "Missing run_id, problem_id, or correct."}, 400
    if history.get_run(run_id, user["id"]) is None:
        # Not this user's run (or it doesn't exist) -- don't let a crafted
        # request grade someone else's history.
        return {"ok": False, "error": "Run not found."}, 404

    history.record_question_result(user["id"], run_id, int(problem_id), topic, bool(correct))
    return {"ok": True}


@app.route("/deadline", methods=["GET", "POST"])
@auth.login_required
def deadline():
    user = auth.current_user()

    if request.method == "GET":
        calendar_error = None
        try:
            candidates = ss.fetch_calendar_candidates(user["id"])
        except Exception as e:
            # The "Upcoming Deadlines" list is a nice-to-have alongside the
            # form, not something that should take the whole page down if
            # Calendar access isn't set up yet or the connection drops.
            candidates = []
            calendar_error = str(e)
        return render_template(
            "deadline_form.html",
            active_step="plan",
            candidates=candidates,
            calendar_error=calendar_error,
        )

    deadline_str = request.form.get("deadline")
    topics_str = request.form.get("topics")
    deadline_date = ss.date.fromisoformat(deadline_str)
    topics = [t.strip() for t in topics_str.split(",") if t.strip()]

    if deadline_date < ss.date.today():
        return "That deadline is in the past. Go back and choose a future date."

    if not topics:
        return "No topics entered. Go back and enter at least one topic."

    skeleton, calendar_note = ss.build_day_skeleton(deadline_date, user["id"])
    if calendar_note:
        # Calendar wasn't connected / couldn't be read -- the plan still
        # gets built (every day just comes back "free"), but say so instead
        # of silently pretending nothing was wrong.
        flash(calendar_note, "error")

    if not any(d["status"] == "free" for d in skeleton):
        return "No free days found before your deadline."

    ss.assign_topics_to_days(skeleton, topics)
    plan_data = ss.generate_deadline_plan_content(skeleton)

    plan_by_date = {p["date"]: p for p in plan_data["plan"]}
    for day in skeleton:
        if day["status"] == "free":
            p = plan_by_date.get(day["date"])
            if p:
                day["topic"] = p["topic"]
                day["task"] = p["task"]

    return render_template("plan.html", skeleton=skeleton, active_step="plan")


@app.route("/paper/custom/pdf", methods=["POST"])
@auth.login_required
def custom_paper_pdf():
    # This is a fetch()-driven endpoint, not a page render -- it returns
    # either a PDF or a small JSON error, never an HTML page, so the
    # Quota/Overload/Network error handlers above (which render error.html)
    # are deliberately bypassed here in favor of plain JSON the front-end
    # JS can show inline without leaving the Practice Paper page. Every
    # generation is also saved (flat list, no folders) so it shows up again
    # later under History -> Custom Papers.
    user = auth.current_user()
    data = request.get_json(silent=True) or {}

    topics_str = (data.get("topics") or "").strip()
    topics = [t.strip() for t in topics_str.split(",") if t.strip()]
    if not topics:
        return {"error": "Enter at least one topic."}, 400

    raw_counts = data.get("counts") or {}

    counts = {}
    total_questions = 0

    # mcq stays a flat count -- it isn't broken down by marks.
    raw_mcq = raw_counts.get("mcq")
    try:
        mcq_n = int(raw_mcq)
    except (TypeError, ValueError):
        mcq_n = 0
    if mcq_n > 0:
        mcq_n = min(mcq_n, 20)  # per-bucket cap
        counts["mcq"] = mcq_n
        total_questions += mcq_n

    # short/long are keyed by marks -- {"1": n, "2": n, "3": n} for short,
    # {"4": n, "5": n, "6": n, "7": n, "10": n} for long. Only keys in
    # ss._CUSTOM_MARKS_BY_TYPE are honored; anything else is ignored rather
    # than rejected, so a front-end quirk doesn't fail the whole request.
    for qtype, allowed_marks in ss._CUSTOM_MARKS_BY_TYPE.items():
        raw_bucket = raw_counts.get(qtype)
        if not isinstance(raw_bucket, dict):
            continue
        bucket = {}
        for marks in allowed_marks:
            raw_n = raw_bucket.get(str(marks), raw_bucket.get(marks))
            try:
                n = int(raw_n)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                n = min(n, 20)  # per-bucket cap
                bucket[str(marks)] = n
                total_questions += n
        if bucket:
            counts[qtype] = bucket

    if not counts:
        return {"error": "Check at least one question type and set how many questions."}, 400
    if total_questions > 40:
        return {"error": "That's a lot of questions for one paper -- try 40 or fewer total."}, 400

    include_or = bool(data.get("include_or"))

    difficulty = (data.get("difficulty") or "medium").strip().lower()
    if difficulty not in ss._CUSTOM_DIFFICULTIES:
        difficulty = "medium"

    try:
        duration_minutes = int(data.get("duration"))
    except (TypeError, ValueError):
        duration_minutes = 60
    if duration_minutes not in (30, 45, 60, 90, 120, 180):
        duration_minutes = 60

    try:
        theory_percent = int(data.get("theory_percent"))
    except (TypeError, ValueError):
        theory_percent = 50
    if theory_percent not in (20, 30, 40, 50, 60, 70, 80):
        theory_percent = 50

    # How many real provider calls this specific paper is about to make --
    # one per (type, marks) bucket, up to 9 for a fully-loaded paper -- so
    # the usage check below can weigh a big paper correctly against the
    # shared daily budget, not just count it as "one generation" like a
    # much cheaper single-call action would be.
    bucket_count = len(list(ss._iter_custom_buckets(counts)))

    try:
        _check_usage_budget(user, bucket_count)
        paper_data, solutions_data = ss.generate_custom_paper_and_solutions(
            topics, counts,
            difficulty=difficulty, duration_minutes=duration_minutes, theory_percent=theory_percent,
            include_or=include_or,
        )
        usage.record_usage(user["id"], bucket_count)
    except ss.QuotaExceededError as e:
        return {"error": str(e)}, 429
    except ss.ModelOverloadedError as e:
        return {"error": str(e)}, 503
    except ss.NetworkError as e:
        return {"error": str(e)}, 503
    except Exception as e:
        # Anything else -- a provider returning a shape the parser/PDF
        # builder didn't expect, etc. -- shouldn't come back as Flask's raw
        # debug page (the front-end can't show that usefully, and it hides
        # what actually happened). Log it and give a clear, generic message
        # instead; see _log_unexpected_error.
        _log_unexpected_error("generate_custom_paper_and_solutions", e)
        return {"error": "Something went wrong generating the paper. Please try again -- if it keeps happening, try a smaller or simpler paper."}, 500

    try:
        pdf_bytes = history.build_custom_paper_pdf(
            topics, counts, paper_data, solutions_data,
            difficulty=difficulty, duration_minutes=duration_minutes, theory_percent=theory_percent,
        )
        history.save_custom_paper(user["id"], topics, counts, pdf_bytes)
    except Exception as e:
        _log_unexpected_error("build_custom_paper_pdf/save_custom_paper", e)
        return {"error": "Your paper was generated but something went wrong building the PDF. Please try again."}, 500

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="custom_practice_paper.pdf",
    )


@app.route("/notes/custom/pdf", methods=["POST"])
@auth.login_required
def custom_notes_pdf():
    # Same fetch()-driven, JSON-error pattern as /paper/custom/pdf. Unlike
    # the custom paper though, every generation here is also saved to the
    # database (folder_id included) so it shows up again later under
    # History -> Custom Notes -> that subject folder.
    user = auth.current_user()
    data = request.get_json(silent=True) or {}

    topics_str = (data.get("topics") or "").strip()
    topics = [t.strip() for t in topics_str.split(",") if t.strip()]
    if not topics:
        return {"error": "Enter at least one topic."}, 400
    if len(topics) > 30:
        return {"error": "That's a lot of topics for one set of notes -- try 30 or fewer."}, 400

    folder_id = data.get("folder_id")
    if folder_id in ("", None, "uncategorized"):
        folder_id = None
    else:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            folder_id = None
        else:
            # Ignore a folder id that doesn't exist / isn't this user's --
            # falls back to Uncategorized rather than erroring out.
            if history.get_note_folder(folder_id, user["id"]) is None:
                folder_id = None

    try:
        _check_usage_budget(user)
        notes_data = ss.generate_custom_notes(topics)
        usage.record_usage(user["id"], 1)
    except ss.QuotaExceededError as e:
        return {"error": str(e)}, 429
    except ss.ModelOverloadedError as e:
        return {"error": str(e)}, 503
    except ss.NetworkError as e:
        return {"error": str(e)}, 503

    pdf_bytes = history.build_custom_notes_pdf(topics, notes_data)
    history.save_custom_notes(user["id"], folder_id, topics, notes_data, pdf_bytes)

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="custom_notes.pdf",
    )


@app.route("/history")
@auth.login_required
def history_page():
    user = auth.current_user()
    return render_template(
        "history.html",
        active_step="history",
        papers_count=history.count_runs_with(user["id"], "paper"),
        solutions_count=history.count_runs_with(user["id"], "solutions"),
        coding_notes_count=history.count_runs_with(user["id"], "notes"),
        custom_notes_count=history.count_all_custom_notes(user["id"]),
        custom_papers_count=history.count_custom_papers(user["id"]),
    )


@app.route("/history/papers")
@auth.login_required
def history_papers():
    user = auth.current_user()
    runs = history.get_runs_with(user["id"], "paper")
    return render_template(
        "history_list.html",
        active_step="history",
        runs=runs,
        list_title="Practice Papers",
        list_subtitle="Every practice paper generated from your weak topics, newest first.",
        empty_title="No practice papers yet.",
        empty_message="Generate a practice paper from the Notes or Paper tab to see it here.",
        delete_endpoint="delete_history_paper",
    )


@app.route("/history/papers/<int:run_id>/delete", methods=["POST"])
@auth.login_required
def delete_history_paper(run_id):
    user = auth.current_user()
    changed = history.delete_run_field(run_id, user["id"], "paper")
    if not changed:
        flash("Couldn't find that practice paper.", "error")
    else:
        flash("Practice paper deleted.", "success")
    return redirect(url_for("history_papers"))


@app.route("/history/solutions")
@auth.login_required
def history_solutions():
    user = auth.current_user()
    runs = history.get_runs_with(user["id"], "solutions")
    return render_template(
        "history_list.html",
        active_step="history",
        runs=runs,
        list_title="Solutions",
        list_subtitle="Every solutions set you've generated, newest first.",
        empty_title="No solutions yet.",
        empty_message="View Solutions on a practice paper to see it here.",
        delete_endpoint="delete_history_solution",
    )


@app.route("/history/solutions/<int:run_id>/delete", methods=["POST"])
@auth.login_required
def delete_history_solution(run_id):
    user = auth.current_user()
    changed = history.delete_run_field(run_id, user["id"], "solutions")
    if not changed:
        flash("Couldn't find that solutions set.", "error")
    else:
        flash("Solutions deleted.", "success")
    return redirect(url_for("history_solutions"))


@app.route("/history/notes")
@auth.login_required
def history_notes():
    user = auth.current_user()
    runs = history.get_runs_with(user["id"], "notes")
    return render_template(
        "history_list.html",
        active_step="history",
        runs=runs,
        list_title="Coding Notes",
        list_subtitle="Your daily auto-generated topic notes, added to every day, newest first.",
        empty_title="No coding notes yet.",
        empty_message="Generate notes from the Dashboard to see them here.",
        delete_endpoint="delete_history_note",
    )


@app.route("/history/notes/<int:run_id>/delete", methods=["POST"])
@auth.login_required
def delete_history_note(run_id):
    user = auth.current_user()
    changed = history.delete_run_field(run_id, user["id"], "notes")
    if not changed:
        flash("Couldn't find that set of coding notes.", "error")
    else:
        flash("Coding notes deleted.", "success")
    return redirect(url_for("history_notes"))


@app.route("/history/custom-papers")
@auth.login_required
def history_custom_papers():
    user = auth.current_user()
    papers = history.get_all_custom_papers(user["id"])
    for p in papers:
        p["composition"] = history.format_custom_paper_composition(p["counts"])
    return render_template(
        "history_custom_papers.html",
        active_step="history",
        papers=papers,
    )


@app.route("/history/custom-papers/pdf/<int:paper_id>")
@auth.login_required
def history_custom_paper_pdf(paper_id):
    user = auth.current_user()
    pdf_bytes = history.get_custom_paper_pdf(paper_id, user["id"])
    if pdf_bytes is None:
        return "Paper not found.", 404
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"custom_practice_paper_{paper_id}.pdf",
    )


@app.route("/history/custom-papers/<int:paper_id>/delete", methods=["POST"])
@auth.login_required
def delete_history_custom_paper(paper_id):
    user = auth.current_user()
    changed = history.delete_custom_paper(paper_id, user["id"])
    if not changed:
        flash("Couldn't find that custom paper.", "error")
    else:
        flash("Custom paper deleted.", "success")
    return redirect(url_for("history_custom_papers"))


@app.route("/history/<int:run_id>/pdf")
@auth.login_required
def history_pdf(run_id):
    user = auth.current_user()
    run_data = history.get_run(run_id, user["id"])
    if run_data is None:
        return "Session not found.", 404
    pdf_bytes = history.build_pdf(run_data)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"solvestreak_run_{run_id}.pdf",
    )


@app.route("/history/custom-notes")
@auth.login_required
def history_custom_notes():
    user = auth.current_user()
    return render_template(
        "history_custom_notes.html",
        active_step="history",
        folders=history.get_note_folders(user["id"]),
        uncategorized_count=history.count_uncategorized_notes(user["id"]),
    )


@app.route("/history/custom-notes/folders", methods=["POST"])
@auth.login_required
def create_note_folder():
    user = auth.current_user()
    name = (request.form.get("name") or "").strip()
    if name:
        history.create_note_folder(user["id"], name)
    else:
        flash("Enter a folder name.", "error")
    return redirect(url_for("history_custom_notes"))


@app.route("/history/custom-notes/folders/<int:folder_id>/rename", methods=["POST"])
@auth.login_required
def rename_note_folder(folder_id):
    user = auth.current_user()
    new_name = (request.form.get("name") or "").strip()
    if new_name:
        changed = history.rename_note_folder(folder_id, user["id"], new_name)
        if not changed:
            flash("Couldn't rename that folder.", "error")
    else:
        flash("Enter a folder name.", "error")
    return redirect(url_for("history_custom_notes"))


@app.route("/history/custom-notes/folder/<int:folder_id>")
@auth.login_required
def history_custom_notes_folder(folder_id):
    user = auth.current_user()
    folder = history.get_note_folder(folder_id, user["id"])
    if folder is None:
        return "Folder not found.", 404
    notes_list = history.get_custom_notes_in_folder(user["id"], folder_id)
    return render_template(
        "history_custom_notes_folder.html",
        active_step="history",
        folder=folder,
        notes_list=notes_list,
    )


@app.route("/history/custom-notes/uncategorized")
@auth.login_required
def history_custom_notes_uncategorized():
    user = auth.current_user()
    notes_list = history.get_custom_notes_in_folder(user["id"], None)
    return render_template(
        "history_custom_notes_folder.html",
        active_step="history",
        folder=None,
        notes_list=notes_list,
    )


@app.route("/history/custom-notes/pdf/<int:note_id>")
@auth.login_required
def history_custom_notes_pdf(note_id):
    user = auth.current_user()
    pdf_bytes = history.get_custom_notes_pdf(note_id, user["id"])
    if pdf_bytes is None:
        return "Notes not found.", 404
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"custom_notes_{note_id}.pdf",
    )


@app.route("/history/custom-notes/<int:note_id>/delete", methods=["POST"])
@auth.login_required
def delete_history_custom_note(note_id):
    user = auth.current_user()
    changed = history.delete_custom_note(note_id, user["id"])
    if not changed:
        flash("Couldn't find that set of notes.", "error")
    else:
        flash("Notes deleted.", "success")
    # Sent back to whichever folder/uncategorized page the delete button was
    # on, via a hidden "next" field in the form -- falls back to the folder
    # browser if that's somehow missing.
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/history/custom-notes"):
        return redirect(next_url)
    return redirect(url_for("history_custom_notes"))


@app.route("/feedback", methods=["GET", "POST"])
@auth.login_required
def feedback_route():
    # Backs the feedback/suggestions popup in the topbar (see base.html) --
    # a fetch()-driven endpoint like /paper/custom/pdf, not a page render,
    # so it always returns JSON rather than redirecting or flashing.
    user = auth.current_user()

    if request.method == "GET":
        return {"items": feedback.list_feedback(user["id"])}

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return {"error": "Write a note before sending."}, 400
    if len(message) > feedback.MAX_MESSAGE_LENGTH:
        return {"error": f"That's a bit long -- keep it under {feedback.MAX_MESSAGE_LENGTH} characters."}, 400

    feedback.save_feedback(user["id"], message)
    return {"ok": True}


@app.route("/feedback/flag", methods=["POST"])
@auth.login_required
def feedback_flag():
    # Backs the "Flag this question" modal (see base.html / solutions.html)
    # -- a separate endpoint from /feedback above because this one also
    # accepts an optional screenshot file, which needs multipart/form-data
    # rather than JSON. Saves into the same feedback.db (tagged with
    # `context`) so admins review everything from one place on /admin.
    user = auth.current_user()

    message = (request.form.get("message") or "").strip()
    context = (request.form.get("context") or "").strip() or None
    if not message:
        return {"error": "Describe what's wrong before sending."}, 400
    if len(message) > feedback.MAX_MESSAGE_LENGTH:
        return {"error": f"That's a bit long -- keep it under {feedback.MAX_MESSAGE_LENGTH} characters."}, 400

    screenshot_bytes = None
    screenshot_mimetype = None
    screenshot_file = request.files.get("screenshot")
    if screenshot_file and screenshot_file.filename:
        if screenshot_file.mimetype not in feedback.ALLOWED_SCREENSHOT_MIMETYPES:
            return {"error": "Screenshots must be PNG, JPEG, WEBP, or GIF."}, 400
        screenshot_bytes = screenshot_file.read()
        if len(screenshot_bytes) > feedback.MAX_SCREENSHOT_BYTES:
            return {"error": "That screenshot is over 5MB -- try a smaller image."}, 400
        screenshot_mimetype = screenshot_file.mimetype

    feedback.save_feedback(user["id"], message, context=context,
                            screenshot_bytes=screenshot_bytes, screenshot_mimetype=screenshot_mimetype)
    return {"ok": True}


@app.route("/admin/feedback/<int:feedback_id>/screenshot")
@auth.login_required
def admin_feedback_screenshot(feedback_id):
    # Admin-only, same "plain 404 instead of 403" reasoning as /admin
    # itself -- this never reveals whether a given feedback id even exists
    # to a non-admin.
    user = auth.current_user()
    if not auth.is_admin(user):
        return render_template(
            "error.html", title="Not found", message="That page doesn't exist.",
            back_href="/", back_label="Back to Dashboard",
        ), 404

    result = feedback.get_feedback_screenshot(feedback_id)
    if result is None:
        return render_template(
            "error.html", title="Not found", message="That screenshot doesn't exist.",
            back_href="/admin", back_label="Back to Admin Dashboard",
        ), 404

    screenshot_bytes, mimetype = result
    return send_file(io.BytesIO(screenshot_bytes), mimetype=mimetype)


# How recently someone must have been seen (see auth.touch_last_seen) to
# count as "active now" on the admin dashboard below. Matches the kind of
# window a person actually means by "active right now" -- long enough that
# someone reading a problem for a couple minutes still counts, short enough
# that someone who closed the tab an hour ago doesn't.
_ADMIN_ACTIVE_WINDOW = timedelta(minutes=5)


@app.route("/admin")
@auth.login_required
def admin_dashboard():
    # Gated behind is_admin, not a separate login system -- everyone signs
    # in the same way, this just decides what a particular signed-in person
    # is allowed to see. A plain 404 (rather than a 403 or a redirect) for
    # non-admins so the route's existence isn't revealed to anyone probing
    # for it; this never changes any other route's behavior for anyone.
    user = auth.current_user()
    if not auth.is_admin(user):
        return render_template(
            "error.html",
            title="Not found",
            message="That page doesn't exist.",
            back_href="/",
            back_label="Back to Dashboard",
        ), 404

    all_users = auth.get_all_users()
    now = datetime.now()

    def _is_active(u):
        if not u["last_seen_at"]:
            return False
        try:
            return now - datetime.fromisoformat(u["last_seen_at"]) < _ADMIN_ACTIVE_WINDOW
        except ValueError:
            return False

    users_display = [
        {**u, "is_active": _is_active(u), "today_generations": usage.get_user_count(u["id"])}
        for u in all_users
    ]
    active_now = sum(1 for u in users_display if u["is_active"])
    users_by_id = {u["id"]: u for u in all_users}

    feedback_items = []
    for item in feedback.list_all_feedback():
        sender = users_by_id.get(item["user_id"])
        feedback_items.append({
            **item,
            "user_name": (sender["name"] or sender["email"] or "Unknown") if sender else "Unknown",
            "user_email": sender["email"] if sender else None,
        })

    return render_template(
        "admin.html",
        active_step="admin",
        total_users=len(all_users),
        active_now=active_now,
        total_feedback=len(feedback_items),
        users=sorted(users_display, key=lambda u: u["created_at"] or "", reverse=True),
        feedback_items=feedback_items,
        today_global_calls=usage.get_global_count(),
        daily_global_call_budget=DAILY_GLOBAL_CALL_BUDGET,
        daily_generations_per_user=DAILY_GENERATIONS_PER_USER,
    )


@app.route("/settings/schedule", methods=["POST"])
@auth.login_required
def settings_schedule():
    # The one shared daily-run time applies app-wide, so only the admin
    # account can change it -- settings.html already hides this form from
    # everyone else, but that's just UI; this is the real guard, since
    # someone could otherwise POST here directly and change it for
    # everyone regardless of what the page shows them.
    if not auth.is_admin(auth.current_user()):
        flash("Only the admin account can change the daily automation time.", "error")
        return redirect(url_for("auth.settings"))

    daily_run_time = request.form.get("daily_run_time", "").strip()
    ok, message = scheduler.set_time(daily_run_time)
    flash(message, "success" if ok else "error")
    return redirect(url_for("auth.settings"))


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
