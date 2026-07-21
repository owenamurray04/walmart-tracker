# Fix package — bad run of 2026-07-20 (cause: Bright Data rate limiting)

## Root cause (confirmed)

Bright Data's unlocker layer started throttling the scraper's traffic
pattern — dominant error `sr_rate_limit` per their support logs. The old
engine was maximally throttle-triggering: 120-call bursts from 24 workers,
near-identical GraphQL requests clustered in the same second, immediate
retries. It passed on 07-15 and got throttled on 07-20 because *their*
sensitivity changed, not the code.

The dashboard damage happened because calls that failed all 3 retries were
silently dropped and every unfetched (store, product) cell counted as
out-of-stock: 78% of calls failed → "417 stores" instead of ~850–900.
Among calls that succeeded, in-stock rates were normal (6.7% vs 7.7% on
07-15) — availability never actually dropped.

## What's in this folder

| File | What it is |
|---|---|
| `weekly_check.py` | Reworked sweep engine (see below) + publish guard. |
| `walmart.yml` | Workflow update: exposes `workers` / `gap` as Run-workflow inputs for tuning without code edits. |
| `history.csv`, `product_history.csv`, `carry_log.csv` | Repo versions minus the bogus 07-20 rows. |

The other dashboard files need no cleanup: the next complete run fully
overwrites them, and `ever_carried.csv` is a union that only ever adds
real sightings.

## The new engine (aligned with Bright Data's guidance)

- 8 workers (was 24), each request launch spaced by a jittered ~0.35s gap —
  no more same-second bursts of identical calls
- adaptive: the gap widens up to 8s when failures spike, relaxes on success
- failed calls retry up to 5× with exponential backoff (15s → 4min), never
  in a tight loop
- call order is shuffled so identical request shapes don't cluster
- checkpoints continuously (~45s); a crashed/killed run resumes free
- **publish guard**: if ANY planned call is missing at the end, it exits
  PARTIAL without touching dashboard files — a throttled day reads as
  "no update", never as a phantom sellout

Tested against a mock proxy: clean run, backoff recovery, permanent-failure
PARTIAL (dashboard untouched), checkpoint resume (no re-billing), budget-cap
PARTIAL, and pacer adaptation all verified.

Note on runtime: gentler pacing may mean a full sweep no longer fits in one
6-hour Actions window. That's fine — it PARTIALs at timeout, commits the
checkpoint, and the next run (manual or next Monday) resumes where it left
off without re-billing. If the canary shows a healthy success rate but slow
pace, you can nudge `workers` up (10–12) in the Run-workflow inputs.

## What to do (in order)

1. **Double-click `Apply Fix.command`** (folder root). It clears the stale
   git locks, pulls, applies these files, commits, pushes, and tidies itself
   into `_to_delete/`. Dashboard shows the last good (07-15) data ~1 min later.
2. **Canary (~$0.30)**: Actions → "1 · Weekly product check" → Run workflow
   with budget **200**. Watch the log's per-minute lines: `done` climbing
   steadily with `gap` holding near 0.35s = healthy; `dropped` climbing and
   `gap` pinned at 8s = still throttled (stop there — it can't hurt the
   dashboard, but don't spend more).
3. **If healthy**: Run workflow again with the default budget (7500). It
   resumes from the canary's checkpoint, so those 200 calls aren't re-billed.
4. **If still throttled**: reply to Bright Data support with the canary run's
   log — steady sub-1-req/s single-flow traffic getting `sr_rate_limit` is
   worth escalating past the bot. Raising `gap` to 1–2s is the next dial.
