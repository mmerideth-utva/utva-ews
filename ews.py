"""UTVA Early Warning System - prototype pipeline.

  python ews.py            ingest roster + Canvas + attendance, run rules, print a run report

Stages: roster (PowerSchool Omni) -> academics (Canvas, every active term, K-12)
-> attendance (attendance team's CA sheet) -> snapshot -> rules -> tiers.
Only aggregate counts are printed; the store holds Student IDs, never names.
"""
import collections
import concurrent.futures
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys

import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("EWS_DB") or os.path.join(ROOT, "ews.db")  # EWS_DB: point tests at a copy
# Prototype reuse: Canvas client + attendance parser live in the passing-rate dashboard
# (a PassingRateDashboard checkout beside this folder, or UTVA_DASHBOARD)
DASH_ROOT = os.environ.get("UTVA_DASHBOARD") or os.path.join(os.path.dirname(ROOT), "PassingRateDashboard")
sys.path.insert(0, DASH_ROOT)
import sync as dash  # noqa: E402
import attendance as dash_att  # noqa: E402
import settings  # noqa: E402

# ews_config.local.json (git-ignored) overrides this laptop's paths, like the dashboard's config.local.json
CFG = settings.load(os.path.join(ROOT, "ews_config.json"), os.path.join(ROOT, "ews_config.local.json"))
TODAY = dt.date.today().isoformat()


def db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(open(os.path.join(ROOT, "schema.sql"), encoding="utf-8").read())
    # Added 2026-10-18: last quarter's results (from the dashboard's quarter archive)
    cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    for col in ("prior_failed", "prior_core_repeat"):
        if col not in cols:
            con.execute(f"ALTER TABLE snapshots ADD COLUMN {col} INTEGER")
    return con


def norm_id(v):
    """Omni stores IDs as int, float (11396661.0) or str depending on the day."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") else s


# ── Stage 1: roster ────────────────────────────────────────────
def month_folder(d):
    """Omni drops land in '<Month YYYY>' subfolders, e.g. 'October 2026'."""
    return os.path.join(CFG["omniFolder"], d.strftime("%B %Y"))


def newest_omni():
    # Current month's folder first; on the 1st (before that month's first drop) fall back to last month
    today = dt.date.today()
    for d in (today, today.replace(day=1) - dt.timedelta(days=1)):
        files = [f for f in glob.glob(os.path.join(month_folder(d), "PowerSchool_Omni*.xlsx"))
                 if not re.search(r"Copy|TEMPLATE|Updated", os.path.basename(f))]
        if files:
            return max(files, key=os.path.getmtime)
    raise FileNotFoundError(f"no PowerSchool_Omni export in {month_folder(today)} or the prior month")


def ingest_roster(con):
    path = newest_omni()
    ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h is not None else "" for h in next(rows)]
    col = {h: i for i, h in enumerate(hdr)}
    out, skipped = [], collections.Counter()
    for r in rows:
        sid = norm_id(r[col["UTVA Student ID"]])
        if not sid:
            skipped["no id"] += 1
            continue
        school = norm_id(r[col["School ID"]])
        if school not in CFG["inScope"]:
            skipped["out of scope"] += 1  # store only what the EWS currently covers
            continue
        g = r[col["Grade Level"]]
        out.append((sid, norm_id(r[col["DCID Canvas ID"]]), int(g) if str(g).lstrip("-").isdigit() else None,
                    school, CFG["schools"].get(school, {}).get("level", "Other"),
                    r[col["Full Time/Part Time"]], r[col["Enrollment Status (Active v W/D)"]],
                    str(r[col["Start Date"]] or "")[:10], str(r[col["Withdrawal Date"]] or "")[:10],
                    r[col["Counselor"]], r[col["Homeroom"]],
                    1 if str(r[col["IEP"]] or "").strip().upper() in ("Y", "YES", "1", "TRUE") else 0,
                    os.path.basename(path)))
    con.execute("DELETE FROM students")
    con.executemany("INSERT OR REPLACE INTO students VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return {"file": os.path.basename(path), "rows": len(out), "skipped": dict(skipped)}


# ── Stage 2: academics (Canvas) ────────────────────────────────
def active_terms(cv):
    resp = cv._open(cv.base + "/accounts/1/terms?per_page=100")
    terms = json.loads(resp.read())["enrollment_terms"]
    now = dt.datetime.now(dt.timezone.utc)
    act = []
    for t in terms:
        s, e = dash.parse_ts(t.get("start_at")), dash.parse_ts(t.get("end_at"))
        if s and e and s <= now <= e and "Archive" not in t["name"]:
            act.append(t)
    return act


def ingest_canvas(con):
    cc = dash.read_json(CFG["canvasConfigPath"])
    cv = dash.Canvas(cc["canvasDomain"], cc["apiToken"])
    terms = active_terms(cv)
    term_ids = {t["id"] for t in terms}
    courses = []
    for school, s in CFG["schools"].items():
        if school not in CFG["inScope"]:
            continue
        for c in cv.get_all(f"/accounts/{s['canvasSubAccount']}/courses?per_page=100&with_enrollments=true"):
            if c.get("enrollment_term_id") in term_ids:
                courses.append((s["level"], c))
    now = dt.datetime.now(dt.timezone.utc)
    window = now - dt.timedelta(days=14)
    per = collections.defaultdict(lambda: {"graded": 0, "failing": 0, "missing": 0, "last": None, "seen": False})
    no_score = collections.Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(dash.fetch_course, cv, c, window, now): (lvl, c) for lvl, c in courses}
        for fut in concurrent.futures.as_completed(futs):
            lvl, c = futs[fut]
            enrollments, missing, _ahead = fut.result()
            # Omni's "DCID Canvas ID" is PowerSchool's DCID, not the Canvas user id, so key
            # everything by Student ID = the numeric prefix of the Canvas login
            sid_of = {e["user_id"]: ((e.get("user") or {}).get("login_id") or "").split("@")[0] for e in enrollments}
            for uid, n in missing.items():
                if sid_of.get(uid):
                    per[sid_of[uid]]["missing"] += n
            for e in enrollments:
                if not sid_of.get(e["user_id"]):
                    continue
                p = per[sid_of[e["user_id"]]]
                p["seen"] = True
                ts = dash.parse_ts(e.get("last_activity_at"))
                if ts and (p["last"] is None or ts > p["last"]):
                    p["last"] = ts
                score = (e.get("grades") or {}).get("current_score")
                if score is None:
                    no_score[lvl] += 1
                    continue
                p["graded"] += 1
                p["failing"] += float(score) < CFG["passingScore"]
    return per, {"terms": [t["name"] for t in terms], "courses": collections.Counter(l for l, _ in courses),
                 "enrollmentsWithoutScore": dict(no_score)}


# ── Stage 3: attendance ────────────────────────────────────────
def ingest_attendance():
    acfg = settings.load()["attendance"]
    label, _src, parsed = dash_att.load_newest(acfg)  # Drive folder first, in memory
    if not parsed:
        return {}, {"source": None}
    meta, _summary, rows = parsed
    return {r["id"]: r for r in rows}, {"source": label, "dataThrough": meta.get("dataThrough"), "rows": len(rows)}


# ── Stage 4: snapshot + rules ──────────────────────────────────
RULE = re.compile(r"^(\w+)\s*(>=|>|<=|<|==)\s*(-?\d+(?:\.\d+)?)$")
OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b,
       "<": lambda a, b: a < b, "==": lambda a, b: a == b}


def evaluate(rule, snap):
    m = RULE.match(rule.strip())
    if not m:
        raise ValueError(f"bad rule: {rule}")
    field, op, val = m.group(1), m.group(2), float(m.group(3))
    v = snap.get(field)
    if field == "days_inactive" and v is None and snap.get("in_canvas"):
        return True, None  # enrolled in Canvas but never active
    return (v is not None and OPS[op](v, val)), v


def ingest_prior():
    """Student ID -> last quarter's results for students in this quarter's courses (dashboard prior_flags.json)."""
    doc = dash.read_json(os.path.join(dash.DATA, "prior_flags.json")) or {}
    return doc.get("students", {}), {"quarter": doc.get("quarter"), "students": len(doc.get("students", {}))}


def build(con, canvas, att, prior=None):
    prior = prior or {}
    scope = ",".join("?" * len(CFG["inScope"]))
    students = con.execute(f"SELECT student_id, canvas_id, level, status FROM students WHERE school_id IN ({scope})",
                           CFG["inScope"]).fetchall()
    con.execute("DELETE FROM snapshots WHERE as_of=?", (TODAY,))
    con.execute("DELETE FROM flags WHERE as_of=?", (TODAY,))
    con.execute("DELETE FROM tiers WHERE as_of=?", (TODAY,))
    now = dt.datetime.now(dt.timezone.utc)
    stats = collections.defaultdict(collections.Counter)
    for sid, canvas_id, level, status in students:
        if str(status).strip().lower() not in ("active", "a"):
            continue
        stats[level]["active"] += 1
        c = canvas.get(sid)
        a = att.get(sid)
        if c and c["seen"]:
            stats[level]["in_canvas"] += 1
        if a:
            stats[level]["on_ca_list"] += 1
        snap = {
            "in_canvas": bool(c and c["seen"]),
            "graded_courses": c["graded"] if c else None,
            "failing_courses": c["failing"] if c else None,
            "missing_14d": c["missing"] if c else None,
            "days_inactive": (now - c["last"]).days if c and c["last"] else None,
            "ca_streak": a["streak"] if a else 0,
            "ca_total_year": a["totalYear"] if a else None,
            "behavior_events": None, "discipline_events": None,
        }
        pr = prior.get(sid)
        con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (TODAY, sid, snap["graded_courses"], snap["failing_courses"], snap["missing_14d"],
                     snap["days_inactive"], snap["ca_streak"], snap["ca_total_year"], None, None,
                     pr["failed"] if pr else 0, len(pr["repeatCore"]) if pr else 0))
    for level, counts in apply_rules(con, TODAY).items():
        stats[level].update(counts)
    return stats


SNAP_COLS = ["graded_courses", "failing_courses", "missing_14d", "days_inactive", "ca_streak", "ca_total_year",
             "behavior_events", "discipline_events", "prior_failed", "prior_core_repeat"]


def snapshot_of(row):
    snap = dict(zip(SNAP_COLS, row))
    snap["in_canvas"] = snap["graded_courses"] is not None
    return snap


def flags_for(snap):
    """Indicator keys (and values) that fire for one snapshot under the current thresholds."""
    out = {}
    for key, ind in CFG["indicators"].items():
        if ind["rule"]:
            hit, val = evaluate(ind["rule"], snap)
            if hit:
                out[key] = val
    return out


def apply_rules(con, as_of):
    """(Re)compute flags and tiers for one snapshot date from stored indicator values."""
    con.execute("DELETE FROM flags WHERE as_of=?", (as_of,))
    con.execute("DELETE FROM tiers WHERE as_of=?", (as_of,))
    stats = collections.defaultdict(collections.Counter)
    rows = con.execute(f"""SELECT s.student_id, u.level, {", ".join("s." + c for c in SNAP_COLS)}
                           FROM snapshots s JOIN students u USING (student_id) WHERE s.as_of=?""", (as_of,))
    for sid, level, *vals in rows.fetchall():
        fired = flags_for(snapshot_of(vals))
        for key, val in fired.items():
            stats[level][key] += 1
            con.execute("INSERT INTO flags VALUES (?,?,?,?)", (as_of, sid, key, val))
        tier = next((t["tier"] for t in CFG["tiers"] if len(fired) >= t["minFlags"]), 0)
        stats[level][f"tier{tier}"] += 1
        if tier:
            con.execute("INSERT INTO tiers VALUES (?,?,?,?)", (as_of, sid, tier, len(fired)))
    return stats


def main():
    con = db()
    if "--rules-only" in sys.argv:  # re-apply thresholds to the latest stored snapshot, no data pull
        as_of = con.execute("SELECT max(as_of) FROM snapshots").fetchone()[0]
        stats = apply_rules(con, as_of)
        con.commit()
        for lvl, c in stats.items():
            print(as_of, lvl, {k: c[k] for k in list(CFG["indicators"]) + ["tier0", "tier1", "tier2", "tier3"]})
        return
    report = {"roster": ingest_roster(con)}
    canvas, report["canvas"] = ingest_canvas(con)
    att, report["attendance"] = ingest_attendance()
    prior, report["priorQuarter"] = ingest_prior()
    stats = build(con, canvas, att, prior)
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                (dt.datetime.now().isoformat(timespec="seconds"), "full", json.dumps(report, default=str)))
    con.commit()
    print(json.dumps(report, indent=1, default=str))
    keys = ["active", "in_canvas", "on_ca_list"] + list(CFG["indicators"]) + ["tier0", "tier1", "tier2", "tier3"]
    print("\n" + "level".ljust(12) + "".join(k[:11].rjust(12) for k in keys))
    for lvl in ("Elementary", "Middle", "High", "Other"):
        if lvl in stats:
            print(lvl.ljust(12) + "".join(str(stats[lvl][k]).rjust(12) for k in keys))


if __name__ == "__main__":
    main()
