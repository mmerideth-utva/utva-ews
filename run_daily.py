"""Daily refresh for the HS dashboard + Early Warning System prototype.

Scheduled weekdays 1:15 PM ("UTVA EWS Daily Refresh") via pythonw.exe (no console window, so
Task Scheduler can't kill it with a console-close signal). Runs after the Omni export
(~6-8:30 AM) and the attendance team's Consecutive Absence sheet (~12:45 PM) have landed.
Log: EWS\\daily-run.log (aggregate counts only, never student IDs).
"""
import datetime as dt
import os
import re
import subprocess
import sys

PY = sys.executable.replace("pythonw.exe", "python.exe")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily-run.log")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
KEEP = re.compile(r'^(level|High|Middle|Elementary)\s|"(source|dataThrough|file)"|Error|Traceback')


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def run(label, script):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([PY, script], cwd=os.path.dirname(script), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW, timeout=75 * 60)
    except subprocess.TimeoutExpired:
        # Log it and let the remaining steps run (they reuse the last good data)
        log(f"{label} TIMED OUT after 75 min")
        return 1
    log(f"{label} exit code {p.returncode}")
    for line in (p.stdout + p.stderr).splitlines():
        if KEEP.search(line):
            log("  " + line.rstrip())
    return p.returncode


def write_health(results, morning):
    """health.json in the shared Drive folder: each step's exit code plus when every source last
    updated (no Student IDs). The Apps Script health check reads it and emails Meghan if something's off."""
    import json
    dash = r"C:\Users\mmerideth\PassingRateDashboard"
    try:
        sys.path.insert(0, dash)
        import sync
        cfg = json.load(open(os.path.join(dash, "config.json"), encoding="utf-8"))
        doc = {"finishedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "run": "morning" if morning else "afternoon", "ok": not any(c for _, c in results),
               "steps": [{"label": l, "code": c} for l, c in results], **sync.freshness()}
        with open(os.path.join(cfg["shareDriveFolder"], "health.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
    except Exception as e:  # the health file must never break the refresh
        log(f"health.json not written: {e!r}")


def wait_for_lock(minutes=60):
    """One refresh at a time. After the laptop sleeps, Windows starts every missed task at once
    (10/12: morning + EWS runs collided on the same files and both failed). The OS releases the
    lock if a run dies, so a crash can't wedge later runs."""
    import msvcrt, time
    fh = open(os.path.join(os.path.dirname(LOG), "run_daily.lock"), "a+")
    deadline = time.time() + minutes * 60
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            if time.time() > deadline:
                return None
            time.sleep(15)


if __name__ == "__main__":
    # --dashboard-only: the 7:30 AM run (fresh grades for the morning); the 1:15 PM run also does the EWS
    morning = "--dashboard-only" in sys.argv
    lock = wait_for_lock()
    if lock is None:
        log("Another refresh held the lock for 60+ min - skipped this run")
        sys.exit(1)
    log("Morning dashboard refresh started" if morning else "Daily refresh started")
    steps = [("Dashboard sync", r"C:\Users\mmerideth\PassingRateDashboard\sync.py"),
             ("Shared copy build", r"C:\Users\mmerideth\PassingRateDashboard\build_share.py"),
             ("Weekly email build", r"C:\Users\mmerideth\PassingRateDashboard\build_email.py")]
    if not morning:
        steps.append(("EWS run", r"C:\Users\mmerideth\EWS\ews.py"))
    codes = [run(label, script) for label, script in steps]
    log("Refresh finished" + ("" if not any(codes) else " WITH ERRORS"))
    write_health([(label, code) for (label, _), code in zip(steps, codes)], morning)
    sys.exit(1 if any(codes) else 0)
