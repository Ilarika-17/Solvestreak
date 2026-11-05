"""
Create, change, check, or remove the Windows Scheduled Task that opens
Solvestreak in your browser automatically before you wake up, starting the
Flask server first if it isn't already running.

This is separate from schedule_task.py, which handles the daily data-refresh
run (fetching LeetCode stats, generating notes/paper/solutions/PDF). This
script is only about having the app already open and waiting on your screen
when you look at your laptop — it doesn't generate anything itself.

Usage (run these from inside the Solvestreak folder):

    python wake_up_task.py              Show the current schedule (or say
                                         there isn't one yet).

    python wake_up_task.py 05:55        Create the task if it doesn't exist
                                         yet, or change the time if it does.
                                         Use 24-hour HH:MM.

    python wake_up_task.py --remove     Delete the scheduled task entirely
                                         (the app will no longer open itself).

Default time if you've never set one: 05:55 (5 minutes before a 6:00 AM
wake-up).

Note: this only works on Windows (it shells out to schtasks.exe), and the
task runs under your Windows user account — your laptop needs to be on and
you need to be logged in (even if locked) at the scheduled time. If you want
it to fire even while the laptop is asleep, open Task Scheduler (search for
it in the Start menu) -> find "SolvestreakWakeUp" -> Properties -> Conditions
tab -> check "Wake the computer to run this task".
"""
import os
import subprocess
import sys

TASK_NAME = "SolvestreakWakeUp"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Prefer pythonw.exe (the windowless twin of python.exe) so the task fires
# silently — no console window flashing up — if it ships alongside the
# interpreter currently running this script; otherwise fall back to the
# normal interpreter.
_pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
PYTHON_EXE = _pythonw if os.path.exists(_pythonw) else sys.executable

SCRIPT_PATH = os.path.join(PROJECT_DIR, "wake_up.py")
DEFAULT_TIME = "05:55"


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
        print("No Solvestreak wake-up schedule is set up yet.")
        print(f"Run:  python wake_up_task.py {DEFAULT_TIME}   (to set the default)")
        return
    print(f"Scheduled task '{TASK_NAME}':")
    for line in result.stdout.splitlines():
        if any(key in line for key in ("Start Time", "Next Run Time", "Status", "Scheduled Task State")):
            print("  " + line.strip())
    print(f"\nTo change the time:  python wake_up_task.py HH:MM")
    print(f"To remove it:        python wake_up_task.py --remove")


def validate_time(hhmm):
    try:
        hh, mm = hhmm.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        print(f"'{hhmm}' doesn't look like a 24-hour HH:MM time, e.g. 05:55 or 06:30.")
        sys.exit(1)


def set_time(hhmm):
    validate_time(hhmm)
    exists = task_exists()

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

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or result.stderr).strip()
    print(output)

    if result.returncode == 0:
        verb = "updated" if exists else "created"
        print(f"\nSolvestreak's wake-up launch has been {verb} — the app will now "
              f"open itself in your browser every day at {hhmm}.")
    else:
        print("\nSomething went wrong setting up the task. If this mentions access/permissions, "
              "try running this from an elevated (Run as Administrator) terminal.")


def remove_task():
    if not task_exists():
        print("There's no SolvestreakWakeUp task to remove.")
        return
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    print((result.stdout or result.stderr).strip())
    if result.returncode == 0:
        print("Removed. The app will no longer open itself automatically.")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        show_current()
    elif sys.argv[1] == "--remove":
        remove_task()
    else:
        set_time(sys.argv[1])
