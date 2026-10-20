"""UTVA EWS prototype - local workflow server (http://127.0.0.1:8766).

Serves the EWS page and a small JSON API over ews.db:
  GET  /api/meta                       indicators, thresholds, strategies, staff, tier counts
  GET  /api/students                   tiered students for the latest snapshot + queue status
  GET  /api/student/<id>               indicator history, flags, interventions, notes
  POST /api/interventions              start a plan        (baseline captured automatically)
  POST /api/interventions/<id>/notes   add a progress note
  POST /api/interventions/<id>/close   close with an outcome (outcome snapshot captured)
Every write lands in the `events` audit table. Student IDs only - no names in this prototype.
There is no sign-in yet: the "acting as" name is self-reported until hosting/SSO is decided.
"""
import datetime as dt
import http.server
import json
import os
import re
import sqlite3
import threading

import ews

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
PORT = int(os.environ.get("EWS_PORT", 8766))
LOCK = threading.Lock()
OUTCOMES = {"improved", "no_change", "worsened", "escalated"}


def conn():
    con = sqlite3.connect(ews.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def seed_strategies():
    with conn() as con:
        con.executescript(open(os.path.join(ROOT, "schema.sql"), encoding="utf-8").read())
        if con.execute("SELECT count(*) FROM strategies").fetchone()[0] == 0:
            lib = json.load(open(os.path.join(ROOT, "strategies.json"), encoding="utf-8"))["strategies"]
            con.executemany("INSERT INTO strategies (name, indicator, tier, evidence, description) VALUES (?,?,?,?,?)",
                            [(s["name"], s["indicator"], s["tier"], s["evidence"], s["description"]) for s in lib])


def latest(con):
    return con.execute("SELECT max(as_of) FROM snapshots").fetchone()[0]


def snapshot(con, sid, as_of):
    row = con.execute(f"SELECT {', '.join(ews.SNAP_COLS)} FROM snapshots WHERE student_id=? AND as_of=?",
                      (sid, as_of)).fetchone()
    if not row:
        return None
    snap = ews.snapshot_of(tuple(row))
    return {"as_of": as_of, "values": {k: snap[k] for k in ews.SNAP_COLS}, "flags": sorted(ews.flags_for(snap))}


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def audit(con, actor, action, sid, ref, detail):
    con.execute("INSERT INTO events (at, actor, action, student_id, ref_id, detail) VALUES (?,?,?,?,?,?)",
                (now(), actor, action, sid, ref, json.dumps(detail)))


class BadRequest(Exception):
    pass


def need(body, *keys):
    for k in keys:
        if not str(body.get(k) or "").strip():
            raise BadRequest(f"{k} is required")


def iso_date(s, field):
    try:
        return dt.date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise BadRequest(f"{field} must be a date (YYYY-MM-DD)")


# ── read endpoints ─────────────────────────────────────────────
def get_meta():
    with conn() as con:
        as_of = latest(con)
        tiers = dict(con.execute("SELECT tier, count(*) FROM tiers WHERE as_of=? GROUP BY tier", (as_of,)).fetchall())
        total = con.execute("SELECT count(*) FROM snapshots WHERE as_of=?", (as_of,)).fetchone()[0]
        staff = [r[0] for r in con.execute(
            "SELECT DISTINCT counselor FROM students WHERE counselor IS NOT NULL AND counselor<>'' ORDER BY 1")]
        strategies = [dict(r) for r in con.execute("SELECT * FROM strategies ORDER BY indicator, tier, name")]
    return {"asOf": as_of, "students": total, "tiers": tiers, "staff": staff, "strategies": strategies,
            "indicators": ews.CFG["indicators"], "tierRules": ews.CFG["tiers"],
            "thresholdsSetOn": ews.CFG.get("thresholdsSetOn"), "defaultReviewDays": ews.CFG["defaultReviewDays"],
            "scopeNote": ews.CFG.get("inScopeNote")}


def get_students():
    today = dt.date.today().isoformat()
    with conn() as con:
        as_of = latest(con)
        rows = con.execute("""
            SELECT t.student_id, t.tier, u.grade, u.counselor,
                   (SELECT group_concat(indicator) FROM flags f WHERE f.as_of=t.as_of AND f.student_id=t.student_id) AS flags,
                   (SELECT count(*) FROM interventions i WHERE i.student_id=t.student_id AND i.status='open') AS open_plans,
                   (SELECT min(review_date) FROM interventions i WHERE i.student_id=t.student_id AND i.status='open') AS next_review
            FROM tiers t JOIN students u USING (student_id) WHERE t.as_of=?""", (as_of,)).fetchall()
        # students with an open plan who have since dropped out of the tiers still need their review
        stale = con.execute("""
            SELECT i.student_id, 0 AS tier, u.grade, u.counselor, '' AS flags, count(*) AS open_plans, min(i.review_date)
            FROM interventions i JOIN students u USING (student_id)
            WHERE i.status='open' AND i.student_id NOT IN (SELECT student_id FROM tiers WHERE as_of=?)
            GROUP BY i.student_id""", (as_of,)).fetchall()
    out = []
    for r in list(rows) + list(stale):
        d = dict(zip(["id", "tier", "grade", "counselor", "flags", "openPlans", "nextReview"], tuple(r)))
        d["flags"] = [f for f in (d["flags"] or "").split(",") if f]
        if d["openPlans"] and d["nextReview"] and d["nextReview"] <= today:
            d["queue"] = "review_due"
        elif d["openPlans"]:
            d["queue"] = "active"
        elif d["tier"] >= 2:
            d["queue"] = "needs_plan"
        else:
            d["queue"] = "monitor"
        out.append(d)
    return {"asOf": as_of, "students": out}


def get_student(sid):
    with conn() as con:
        s = con.execute("SELECT student_id, grade, counselor, homeroom, status FROM students WHERE student_id=?",
                        (sid,)).fetchone()
        if not s:
            return None
        history = [snapshot(con, sid, r[0]) for r in
                   con.execute("SELECT as_of FROM snapshots WHERE student_id=? ORDER BY as_of", (sid,))]
        tier = con.execute("SELECT tier FROM tiers WHERE student_id=? AND as_of=?", (sid, latest(con))).fetchone()
        plans = []
        for p in con.execute("""SELECT i.*, s.name AS strategy, s.description AS strategy_desc FROM interventions i
                                LEFT JOIN strategies s USING (strategy_id) WHERE student_id=?
                                ORDER BY status='open' DESC, start_date DESC""", (sid,)):
            p = dict(p)
            p["baseline"] = json.loads(p["baseline"] or "null")
            p["outcome_snapshot"] = json.loads(p["outcome_snapshot"] or "null")
            p["notes"] = [dict(n) for n in con.execute(
                "SELECT * FROM intervention_notes WHERE intervention_id=? ORDER BY created_at", (p["intervention_id"],))]
            plans.append(p)
    return {"student": dict(s), "tier": tier[0] if tier else 0, "history": history, "plans": plans}


# ── write endpoints ────────────────────────────────────────────
def create_intervention(b):
    need(b, "student_id", "indicator", "strategy_id", "owner", "review_date", "actor")
    if b["indicator"] not in ews.CFG["indicators"]:
        raise BadRequest("unknown indicator")
    review = iso_date(b["review_date"], "review_date")
    if review <= dt.date.today().isoformat():
        raise BadRequest("review_date must be in the future")
    with LOCK, conn() as con:
        if not con.execute("SELECT 1 FROM students WHERE student_id=?", (b["student_id"],)).fetchone():
            raise BadRequest("unknown student")
        if not con.execute("SELECT 1 FROM strategies WHERE strategy_id=?", (b["strategy_id"],)).fetchone():
            raise BadRequest("unknown strategy")
        as_of = latest(con)
        tier = con.execute("SELECT tier FROM tiers WHERE student_id=? AND as_of=?", (b["student_id"], as_of)).fetchone()
        base = snapshot(con, b["student_id"], as_of)
        cur = con.execute("""INSERT INTO interventions (student_id, tier, indicator, strategy_id, goal, owner, start_date,
                             review_date, status, baseline, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,'open',?,?,?)""",
                          (b["student_id"], tier[0] if tier else 0, b["indicator"], int(b["strategy_id"]),
                           (b.get("goal") or "").strip(), b["owner"].strip(), dt.date.today().isoformat(), review,
                           json.dumps(base), b["actor"].strip(), now()))
        audit(con, b["actor"], "intervention.create", b["student_id"], cur.lastrowid,
              {"indicator": b["indicator"], "strategy_id": b["strategy_id"], "owner": b["owner"]})
        return {"intervention_id": cur.lastrowid}


def add_note(iid, b):
    need(b, "author", "body")
    audience = b.get("audience") or "staff"
    if audience not in ("staff", "parent-visible"):
        raise BadRequest("audience must be staff or parent-visible")
    with LOCK, conn() as con:
        p = con.execute("SELECT student_id FROM interventions WHERE intervention_id=?", (iid,)).fetchone()
        if not p:
            raise BadRequest("unknown intervention")
        cur = con.execute("INSERT INTO intervention_notes (intervention_id, author, audience, body, created_at) "
                          "VALUES (?,?,?,?,?)", (iid, b["author"].strip(), audience, b["body"].strip(), now()))
        audit(con, b["author"], "note.add", p[0], iid, {"note_id": cur.lastrowid, "audience": audience})
        return {"note_id": cur.lastrowid}


def close_intervention(iid, b):
    need(b, "outcome", "actor")
    if b["outcome"] not in OUTCOMES:
        raise BadRequest("outcome must be one of " + ", ".join(sorted(OUTCOMES)))
    with LOCK, conn() as con:
        p = con.execute("SELECT student_id, status FROM interventions WHERE intervention_id=?", (iid,)).fetchone()
        if not p:
            raise BadRequest("unknown intervention")
        if p[1] != "open":
            raise BadRequest("intervention is already closed")
        snap = snapshot(con, p[0], latest(con))
        con.execute("""UPDATE interventions SET status='closed', outcome=?, outcome_snapshot=?, closed_at=?, closed_by=?
                       WHERE intervention_id=?""", (b["outcome"], json.dumps(snap), now(), b["actor"].strip(), iid))
        if (b.get("note") or "").strip():
            con.execute("INSERT INTO intervention_notes (intervention_id, author, audience, body, created_at) "
                        "VALUES (?,?,'staff',?,?)", (iid, b["actor"].strip(), "Closed: " + b["note"].strip(), now()))
        audit(con, b["actor"], "intervention.close", p[0], iid, {"outcome": b["outcome"]})
        return {"ok": True}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/meta":
            return self._json(get_meta())
        if self.path == "/api/students":
            return self._json(get_students())
        m = re.fullmatch(r"/api/student/(\d+)", self.path)
        if m:
            s = get_student(m.group(1))
            return self._json(s) if s else self._json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._json({"error": "forbidden"}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/interventions":
                return self._json(create_intervention(body), 201)
            m = re.fullmatch(r"/api/interventions/(\d+)/(notes|close)", self.path)
            if m:
                fn = add_note if m.group(2) == "notes" else close_intervention
                return self._json(fn(int(m.group(1)), body), 201)
            return self._json({"error": "not found"}, 404)
        except BadRequest as e:
            return self._json({"error": str(e)}, 400)
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "invalid request"}, 400)


if __name__ == "__main__":
    seed_strategies()
    print(f"UTVA EWS prototype: http://127.0.0.1:{PORT}/", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
