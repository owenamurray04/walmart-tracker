# Fix 3 — probe-gated schedule (2026-07-21)

Bright Data's throttle on the Walmart pattern is reputation-based (their
support's description): hammering a degraded pattern keeps it degraded.
So the schedule now self-heals instead of grinding:

- cron fires every 6h Mon–Wed (UTC)
- each firing PROBES first: ~10 min of single-file calls at a 5s pace
  (~25–70 attempts). Still throttled -> exits immediately (red X = "still
  throttled", costs ~nothing, dashboard untouched). Healthy -> ramps to
  8 workers / 0.35s and runs the real sweep in the same run
- checkpoint chains across firings; publish guard unchanged; after the
  week's sweep completes, remaining firings no-op via the fresh-window
  guard (green, seconds, free)

Net effect: the moment Bright Data recovers, the next firing (≤6h later)
completes and publishes the week automatically. While they're broken, your
total pressure is ~100 gentle attempts per firing — the opposite of
feeding the throttle. Manual runs from the Actions tab skip the probe.

Apply with "Apply Fix 3.command". Then there is genuinely nothing to do
but wait for a green run — and keep the human support ticket going.
