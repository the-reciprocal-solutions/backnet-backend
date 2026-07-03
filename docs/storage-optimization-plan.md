# Storage Optimization Plan — TimescaleDB

_Plan only — no code changes yet. Grounded in the live DB state measured on the running fleet._

## Where we are (measured)

| Fact | Value |
|---|---|
| Hypertable total | ~1266 MB |
| Fleet write rate | ~58.7 MB/day, ~1.16 M rows/day |
| Sampling | every point on a **5 s** fixed grid |
| Chunk size | 1 day |
| Compression | **enabled**, `segmentby=point_id`, after **7 days** |
| Compression result | 16/24 chunks compressed: **2060 MB → 101 MB (~95%)** |
| Retention | raw dropped after **90 days** |
| Continuous aggregates | 1m / 15m / 1h rollups exist |

**Key finding:** compression already works extremely well (95%). The bulk of the
current 1266 MB is the **7-day uncompressed hot window plus indexes** — recent
chunks that haven't been compressed yet. So the two real levers are:
1. **Compress the hot window sooner** (shrink the uncompressed tail).
2. **Write fewer rows** (5 s sampling stores every point every tick, even when
   nothing changed — the largest structural waste).

Everything below is ordered by impact-to-effort.

---

## Phase 1 — Quick wins (config only, low risk)

### 1.1 Compress sooner — `compress_after` 7d → 1d
Chunks are 1 day. Compressing after 1 day (instead of 7) collapses ~6 days of
the hot window at ~95%. On today's data that is roughly **1.1 GB → ~60 MB** for
those chunks.
- **Change:** `add_compression_policy('point_reading', INTERVAL '1 day')`.
- **Saving:** ~1 GB now; keeps steady-state footprint an order of magnitude smaller.
- **Risk:** low. Compressed chunks are read-only — late-arriving writes to a
  compressed chunk need decompress. Our writes are always "now", so safe. Keep a
  1-day uncompressed buffer for the current chunk.
- **Effort:** 1 line in the schema policy list.

### 1.2 Shorten raw retention — 90d → 14–30d, lean on rollups
Forecast/copilot/history already read from 1m/15m/1h continuous aggregates. Raw
5 s data older than ~2 weeks has little value and dominates chunk count.
- **Change:** `add_retention_policy('point_reading', INTERVAL '14 days')` (tune);
  raise cagg retention so long-range history/charts still work.
- **Saving:** caps raw at ~14× daily (post-compression ~1–2 GB steady) instead of 90×.
- **Risk:** low–medium — anything needing sub-minute detail older than the window
  is gone. Confirm no consumer needs raw beyond the window.
- **Effort:** config + verify cagg retention.

### 1.3 Raise the sample grid — 5s → 15–30s
5 s is finer than most BMS points need (temps, setpoints, valves move slowly).
15 s cuts row volume 3×; 30 s cuts 6× — linearly across writes, index, compressed
and uncompressed size.
- **Change:** `BACNET_LAB_TSDB_SAMPLE_INTERVAL_S` (already an env knob).
- **Saving:** 3–6× fewer rows fleet-wide.
- **Risk:** low — lose sub-15s resolution. Fast points (vibration) may want their own faster rate (see 2.2).
- **Effort:** one env var.

---

## Phase 2 — Structural (code, higher impact)

### 2.1 Store-on-change (deadband / COV) — the big one
Today the historian writes **every point every tick** regardless of whether the
value moved. Most BMS points are flat most of the time (binaries, setpoints,
slow temps). Writing only when a value changes beyond a per-point **deadband**
(COV increment — we already store `cov_increment` per point) typically cuts rows
**70–95%** for slow/steady points.
- **Change:** in the historian sample loop, keep a last-stored value per point;
  emit a row only if `abs(value - last) >= deadband` (or on boolean flip, or a
  heartbeat every N minutes so gaps are explicit).
- **Saving:** largest single win — reduces writes, index, compressed AND
  uncompressed size, and write CPU. Compounds with 1.1/1.3.
- **Risk:** medium — irregular sampling. Charts/forecast must handle uneven
  spacing (the caggs already bucket by time, so they cope). Add a periodic
  heartbeat write so "no data" vs "unchanged" is distinguishable.
- **Effort:** moderate — historian loop change + a heartbeat interval + tests.

### 2.2 Per-point sample rates / tiers
Not every point deserves the same cadence. Tier points: fast (vibration, power)
at 1–5 s, normal (temps) at 30 s, slow (setpoints, status) on-change only.
- **Change:** optional `sample_interval_s` / `store_mode` per point in config +
  historian honoring it.
- **Saving:** targets spend where resolution matters; big cut on the long tail.
- **Risk:** low. **Effort:** moderate; do after 2.1.

### 2.3 Narrow the row
`point_reading` has `value_num` (float8), `value_bool`, `value_text`, `quality`
(smallint) — only one value column is used per row, and `quality` is effectively
constant.
- **Options:** drop/parameterize `quality`; consider `float4` for `value_num`
  (halves numeric, adequate for sensor precision). Note compression already
  reclaims most of this, so treat as minor / do only if convenient.
- **Saving:** small post-compression. **Risk:** low (schema migration).
  **Effort:** low but migration-gated — lowest priority.

---

## Phase 3 — Operational guardrails

- **Storage budget alerting** — use the new `GET /api/timeseries/storage` to
  alert when fleet MB/day or total crosses a threshold.
- **Downsampling policy for very old data** — keep 1h cagg for a year+, drop
  finer rollups on a schedule.
- **Per-device caps** — if a single high-point-count device dominates (AHU-01 =
  11 MB/day), consider tiering its points (2.2).
- **Verify compression health** — periodically check
  `hypertable_compression_stats` so a stalled compression job doesn't silently
  balloon the hot window.

---

## Recommended order & expected effect

| Step | Type | Effort | Expected footprint effect |
|---|---|---|---|
| 1.1 compress after 1 day | config | XS | **~1 GB reclaimed now** |
| 1.3 sample 5s→15/30s | env | XS | 3–6× fewer rows |
| 1.2 raw retention 90d→14–30d | config | S | caps steady-state at ~1–2 GB |
| 2.1 store-on-change (COV) | code | M | **70–95% fewer rows** on slow points |
| 2.2 per-point tiers | code | M | trims the long tail |
| 2.3 narrow row | migration | S | minor (post-compression) |

Do 1.1 + 1.3 first (minutes, ~immediate GB back), then 2.1 for the structural
win. Combined, steady-state storage should drop from a 90-day/5s/all-points
regime to roughly a **10–20× smaller** footprint with no loss of usable history.

## Open questions before implementing
- What is the **longest window any consumer needs raw (sub-minute) data**? Sets 1.2.
- Acceptable **deadband per point class** (temp ±0.1°C? %±0.5?)? Sets 2.1.
- Any point that genuinely needs **5 s** resolution? Sets 1.3 / 2.2.
