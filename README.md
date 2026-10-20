# UTVA Early Warning System: prototype (HS only)

A local prototype for working through the architecture of a self-built EWS under Utah Code 53F-4-207 and
R277-627. Before any of this is used as compliance evidence, verify those citations and the current statute
text against the official sources.

**Scope:** Full-Time high school (school 700) only. Elementary and Middle need a different course-failure
measure, and SOEP gets its own dashboard. Change `inScope` in `ews_config.json` to widen it.

## Run
- `python ews.py`: full pull (PowerSchool Omni + Canvas + attendance), then snapshot, flags and tiers.
- `python ews.py --rules-only`: re-applies the thresholds to the latest stored snapshot with no data pull.
  Use it after editing thresholds.
- `python server.py` serves http://127.0.0.1:8766/ (Queue, Student detail with plans, Strategy library).

## Pieces
| File | What it does |
|---|---|
| `ews_config.json` | Scope, indicator rules (statute vs. local criteria), tier rules, review default |
| `schema.sql` | SQLite store: students, snapshots, flags, tiers, strategies, interventions, notes, events (audit) |
| `ews.py` | Ingest and rules engine. Omni comes from the **current month's** folder (`<Month YYYY>`) under the Daily Reports SY2627 folder (Drive id `1EgNH0iFNVbbUNfufU89IAiHLMXWlAAoo`). Canvas uses every term active today. |
| `server.py` | Workflow API: create plan (baseline captured), add note, close with outcome (outcome snapshot captured). Every write is audited. |
| `strategies.json` | Starter strategy library. The evidence sources are **not yet verified**. |

## Data handling
- Stores **Student IDs only**: no names, contact details, or demographics. The IEP flag is kept solely for
  the 30-day attendance rule and is never displayed.
- Students are matched to Canvas by the Student ID prefix of their Canvas login. Omni's "DCID Canvas ID" is
  PowerSchool's DCID, not the Canvas user id.
- There is no sign-in yet. The "acting as" name is self-reported, so the audit trail can't be trusted until
  hosting and SSO are decided.
- Test against a copy of the database with `EWS_DB=<copy> EWS_PORT=8799 python server.py`.
