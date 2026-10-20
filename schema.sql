-- UTVA Early Warning System - prototype store (SQLite).
-- ID-only by design: no names, contact info, or demographics until hosting/access is decided.

CREATE TABLE IF NOT EXISTS students (
  student_id      TEXT PRIMARY KEY,   -- UTVA Student ID (PowerSchool)
  canvas_id       TEXT,               -- join key into Canvas
  grade           INTEGER,            -- 0 = K
  school_id       TEXT,
  level           TEXT,               -- Elementary / Middle / High
  ft_pt           TEXT,
  status          TEXT,               -- Active / W/D
  start_date      TEXT,
  withdrawal_date TEXT,
  counselor       TEXT,               -- staff name: drives caseload views later
  homeroom        TEXT,
  iep             INTEGER,            -- needed for the 30-day attendance limit; never displayed
  roster_date     TEXT                -- Omni report the row came from
);

-- One row per student per run: the raw indicator values the rules engine reads
CREATE TABLE IF NOT EXISTS snapshots (
  as_of            TEXT NOT NULL,
  student_id       TEXT NOT NULL,
  graded_courses   INTEGER,
  failing_courses  INTEGER,
  missing_14d      INTEGER,
  days_inactive    INTEGER,            -- NULL = never active in Canvas
  ca_streak        INTEGER,
  ca_total_year    INTEGER,
  behavior_events  INTEGER,            -- NULL until a source exists
  discipline_events INTEGER,           -- NULL until a source exists
  PRIMARY KEY (as_of, student_id)
);

-- Rules engine output: which indicators fired, and the resulting tier
CREATE TABLE IF NOT EXISTS flags (
  as_of       TEXT NOT NULL,
  student_id  TEXT NOT NULL,
  indicator   TEXT NOT NULL,
  value       REAL,
  PRIMARY KEY (as_of, student_id, indicator)
);
CREATE TABLE IF NOT EXISTS tiers (
  as_of       TEXT NOT NULL,
  student_id  TEXT NOT NULL,
  tier        INTEGER NOT NULL,
  flag_count  INTEGER NOT NULL,
  PRIMARY KEY (as_of, student_id)
);

-- Intervention workflow (Phase 2) - schema only for now, so the model can be reviewed
CREATE TABLE IF NOT EXISTS strategies (
  strategy_id INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  indicator   TEXT,                    -- which indicator it addresses
  tier        INTEGER,
  evidence    TEXT,                    -- source / evidence base
  description TEXT
);
CREATE TABLE IF NOT EXISTS interventions (
  intervention_id INTEGER PRIMARY KEY,
  student_id   TEXT NOT NULL,
  tier         INTEGER NOT NULL,
  indicator    TEXT NOT NULL,
  strategy_id  INTEGER REFERENCES strategies(strategy_id),
  goal         TEXT,                   -- what "worked" looks like, set at the start
  owner        TEXT NOT NULL,          -- staff member responsible
  start_date   TEXT NOT NULL,
  review_date  TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open',   -- open / closed
  baseline     TEXT,                   -- JSON: indicator values when the plan started
  outcome      TEXT,                   -- improved / no_change / worsened / escalated
  outcome_snapshot TEXT,               -- JSON: indicator values when it closed (effectiveness)
  closed_at    TEXT,
  closed_by    TEXT,
  created_by   TEXT,
  created_at   TEXT
);
CREATE TABLE IF NOT EXISTS intervention_notes (
  note_id         INTEGER PRIMARY KEY,
  intervention_id INTEGER NOT NULL REFERENCES interventions(intervention_id),
  author          TEXT NOT NULL,
  audience        TEXT NOT NULL DEFAULT 'staff',  -- staff / parent-visible
  body            TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

-- Audit trail: every write to the workflow (who, what, when)
CREATE TABLE IF NOT EXISTS events (
  event_id    INTEGER PRIMARY KEY,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,          -- intervention.create / note.add / intervention.close
  student_id  TEXT,
  ref_id      INTEGER,
  detail      TEXT
);

-- Ingest run log: what came in, from where, and what didn't match
CREATE TABLE IF NOT EXISTS runs (
  run_at   TEXT PRIMARY KEY,
  source   TEXT,
  detail   TEXT
);
