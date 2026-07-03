# Storage Benchmark — Per-Device Time-Series Footprint

_Source: live TimescaleDB (`point_reading` hypertable) via `GET /api/timeseries/storage`. Snapshot of the running simulator fleet._

## Summary

| Metric | Value |
|---|---|
| Hypertable total size (data + indexes, all chunks) | **1266.3 MB** (≈ 1.27 GB) |
| Estimated rows stored | ~24.9 M |
| Average bytes per reading (incl. index) | **53.24 B** |
| Fleet readings per day | ~1.156 M |
| **Fleet growth per day** | **58.7 MB/day** |

At the current sampling rate the fleet writes roughly **59 MB/day**. Against the
90-day retention policy that trends to a steady-state of **~5.2 GB** before
compression, and materially less once the compression policy (chunks older than
7 days) kicks in.

## Per-device storage per day

Ordered by daily growth. `bytes/day = readings/day × 53.24 B/reading`.

| Device ID | Name | Points | Readings/day | MB/day |
|---|---|---|---|---|
| 1001 | AHU-01 | 13 | 221,000 | **11.22** |
| 6001 | PM-01 | 11 | 187,000 | 9.50 |
| 2002 | FCU-02 | 7 | 119,000 | 6.04 |
| 4001 | ZC-01 | 7 | 119,000 | 6.04 |
| 2001 | FCU-01 | 7 | 119,000 | 6.04 |
| 3001 | TSTAT-01 | 6 | 102,000 | 5.18 |
| 7003 | KNX-RTC-01 | 5 | 85,000 | 4.32 |
| 7001 | KNX-LIGHT-01 | 4 | 68,000 | 3.45 |
| 5002 | CO2-01 | 3 | 51,000 | 2.59 |
| 7002 | KNX-BLIND-01 | 3 | 51,000 | 2.59 |
| 5001 | OAT-01 | 2 | 34,000 | 1.73 |
| 7004 | KNX ETS Import (KV.knxproj) | 53 | 0 | 0.00 |
| 7005 | KNX Import (KV v2.5 - demo) | 13 | 0 | 0.00 |

Daily footprint scales linearly with **point count** — each point is sampled on
the same fixed grid, so `MB/day ≈ points × 0.86 MB`. AHU-01 (13 points) is the
heaviest live device at 11.2 MB/day.

The two ETS-imported KNX devices (7004, 7005) show **0 readings/day**: KNX is
disabled in this environment (`BACNET_LAB_KNX_ENABLED=false`), so no live
telegrams arrive and the historian records nothing for them. Once a KNX gateway
is connected they would begin contributing at ~0.86 MB/day per point.

## How it is measured (from the DB)

The `/api/timeseries/storage` endpoint (backed by `TimescaleTimeSeries.storage_by_device`)
computes:

1. **Total on-disk size** — `hypertable_size('point_reading')`, which sums every
   child chunk plus indexes. (`pg_total_relation_size` on the hypertable parent
   returns ~0 because the rows live in chunk tables, not the parent.)
2. **Row count** — sum of `reltuples` across the hypertable's chunks (a fast
   catalog estimate; falls back to `count(*)` if unavailable). The parent's own
   `reltuples` is 0.
3. **Bytes per reading** — total size ÷ row count.
4. **Readings/day per device** — `count(point_reading)` over the last 24 h,
   grouped by device (Timescale chunk-exclusion keeps this cheap).
5. **Bytes/day per device** — readings/day × bytes/reading.

## Caveats

- Figures are **estimates**. Timescale stores rows by time chunk, not by device,
  so exact per-device on-disk bytes cannot be queried directly; readings/day ×
  average-bytes/reading is the standard approximation.
- `bytes/reading` (53.24 B) includes the `ix_reading_point_time` index and blends
  compressed and uncompressed chunks, so it drifts as the compression policy
  runs on older chunks.
- Readings/day uses a rolling 24-hour window, so a device onboarded mid-window
  reads low until it has a full day of history.

## Endpoint

```bash
curl -u admin:admin123 https://bacnet.tools.thefusionapps.com/api/timeseries/storage
```

Returns `{summary: {...}, devices: [{device_id, name, points, rows_per_day,
bytes_per_day, mb_per_day, bytes_per_reading}]}`.
