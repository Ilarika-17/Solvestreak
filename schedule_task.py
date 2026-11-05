"""
Create, change, check, or remove the Windows Scheduled Task that runs
daily_run.py automatically every day. This is the "change the timing"
feature — the schedule lives in Windows Task Scheduler, and this script
is just a convenient way to point/repoint it without opening the GUI.

Usage (run these from inside the Solvestreak folder):

    python schedule_task.py              Show the current schedule (or say
                                          there isn't one yet).

    python schedule_task.py 06:00        Create the task if it doesn't exist
                                          yet, or change the time if it does.
                                          Use 24-hour HH:MM, e.g. 06:00, 19:30.

    python schedule_task.py --remove     Delete the scheduled task entirely
                                          (daily_run.py will no longer run on
                                          its own).

Default time if you've never set one: 06:00.

Note: this only works on Windows (it shells out to schtasks.exe), and the
task runs under your Windows user account — your PC needs to be on and you
need to be logged in (even if locked) at the scheduled time. If you want it
to fire even while the PC is asleep, open Task Scheduler (search for it in
the Start menu) -> find "SolvestreakDaily" -> Properties -> Conditions tab ->
check "Wake the computer to run this task".
"""
import json
import os
import subprocess
import sys

TASK_NAME = "SolvestreakDaily"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXE = sys.executable
SCRIPT_PATH = os.path.join(PROJECT_DIR, "daily_run.py")
DEFAULT_TIME = "06:00"
CONFIG_PATH = os.path.join(PROJECT_DIR, "automation_config.json")


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(hhmm):
    config = _load_config()
    config["daily_run_time"] = hhmm
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass  # non-fatal -- the actual schtasks change already succeeded


def get_saved_time():
    """The last time successfully set via set_time() (CLI or the web
    Settings page), for display there. Falls back to the default if nothing
    has been saved yet."""
    return _load_config().get("daily_run_time", DEFAULT_TIME)


def task_exists():
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def show_current():
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("No daily Solvestreak schedule is set up yet.")
        print(f"Run:  python schedule_task.py {DEFAULT_TIME}   (to set the default)")
        return
    print(f"Scheduled task '{TASK_NAME}':")
    for line in result.stdout.splitlines():
        if any(key in line for key in ("Start Time", "Next Run Time", "Status", "Scheduled Task State")):
            print("  " + line.strip())
    print(f"\nTo change the time:  python schedule_task.py HH:MM")
    print(f"To remove it:        python schedule_task.py --remove")


def _parse_time(hhmm):
    """Returns True if hhmm is a valid 24-hour "HH:MM" string."""
    try:
        hh, mm = hhmm.split(":")
        hh, mm = int(hh), int(mm)
        return 0 <= hh <= 23 and 0 <= mm <= 59
    except (ValueError, AttributeError):
        return False


def set_time(hhmm):
    """Create or update the SolvestreakDaily scheduled task.

    Returns (ok, message) and never exits the process -- this is called both
    from the command line (see __main__ below, which prints the message and
    exits on failure) and from the web Settings page's /settings/schedule
    route, where exiting the process would take down the whole running app.
    """
    if not _parse_time(hhmm):
        return False, f"'{hhmm}' doesn't look like a 24-hour HH:MM time, e.g. 06:00 or 19:30."

    try:
        exists = task_exists()
    except FileNotFoundError:
        return False, "schtasks isn't available here -- this only works on Windows."

    if exists:
        cmd = ["schtasks", "/Change", "/TN", TASK_NAME, "/ST", hhmm]
    else:
        cmd = [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", f'"{PYTHON_EXE}" "{SCRIPT_PATH}"',
            "/SC", "DAILY",
            "/ST", hhmm,
            "/F",
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "schtasks isn't available here -- this only works on Windows."

    output = (result.stdout or result.stderr).strip()

    if result.returncode == 0:
        _save_config(hhmm)
        verb = "updated" if exists else "created"
        return True, f"Solvestreak's daily run has been {verb} — it will now run automatically every day at {hhmm}."

    hint = ""
    if "access" in output.lower() or "permission" in output.lower() or "denied" in output.lower():
        hint = " Try running this from an elevated (Run as Administrator) terminal."
    return False, f"Something went wrong setting up the task.{hint} ({output})" if output else (
        f"Something went wrong setting up the task.{hint}"
    )


def remove_task():
    if not task_exists():
        print("There's no SolvestreakDaily task to remove.")
        return
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    print((result.stdout or result.stderr).strip())
    if result.returncode == 0:
        print("Removed. daily_run.py will no longer run automatically.")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        show_current()
    elif sys.argv[1] == "--remove":
        remove_task()
    else:
        ok, message = set_time(sys.argv[1])
        print(message)
        if not ok:
            sys.exit(1)
