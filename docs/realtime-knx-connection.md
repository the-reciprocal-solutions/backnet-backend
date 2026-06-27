# Real-Time KNX Connection — What Works Now

_Status as of this branch (`feat/knx-ets-timeseries`). Scope: live KNX device communication, ETS-based discovery, data fetch, and time-series storage._

## Summary

The backend can now talk to a **real KNX installation** end-to-end: discover points from an ETS export, subscribe to their group addresses on the KNX bus, ingest live values, and store them in TimescaleDB. The data path is wired and unit-tested; it has **not** been validated against physical KNX hardware (no KNXnet/IP gateway available in this environment).

| Capability | State |
|---|---|
| Discover points via ETS file | ✅ works |
| Connect to KNX bus (KNXnet/IP) | ✅ implemented (xknx), needs a gateway to go live |
| Send (write) to a group address | ✅ works (DPT-encoded telegram) |
| Receive (read) live values | ✅ implemented (inbound telegram listener + GroupValueRead) |
| Store readings in time-series DB | ✅ works (imported device auto-registered in TimescaleDB) |
| Surface live status to UI | ✅ `/api/discovery` → `knx_status`; Discovery page badge |
| Validated on real hardware | ❌ not yet (no gateway) |

---

## The real-time data flow

```
ETS file (.knxproj/.xml/.csv)
        │  POST /api/discovery/import/ets
        ▼
  ETS parser  ──► KNX Device (points carry group_address + dpt)
        │
        ├──► save_device         (SQLite + in-memory)
        ├──► activate_device     (KnxEngine.start_device → subscribe group addresses)
        └──► tsdb.register_devices  (TimescaleDB point rows)
                                 │
   KNX bus  ──telegram──►  KnxEngine._on_telegram
   (real device sends      │  decode by DPT
    GroupValueWrite on     ▼
    change)            in-memory Point.present_value updated
                                 │
                  Historian sample loop (regular grid)
                                 ▼
                      TimescaleDB point_reading
                                 │
                 /api/history · /api/timeseries · /api/simulation/snapshot
                                 ▼
                          Frontend / clients
```

Key point: the KNX listener updates the **same in-memory Point object** the historian samples, so a real bus value flows to storage with no extra plumbing. ETS-imported points have no simulation model, so the simulation engine does not overwrite them.

---

## What each piece does

### 1. Discovery via ETS (`src/bacnet_lab/adapters/knx/ets_import.py`)
KNX has no runtime auto-discovery (unlike BACnet `Who-Is`) — the **ETS project is the source of truth**. The parser reads ETS group-address exports (`.csv`, `.xml`, `.knxproj`), extracting each group address (`1/2/3`), its name, and DPT (e.g. `DPST-9-1` → `9.001`). Stdlib only.

### 2. Import endpoint (`POST /api/discovery/import/ets`)
Uploads a file, builds one Point per group address (DPT main `1` → binary, else analog), persists the device, then **activates** it (subscribes its group addresses on the KNX engine) and **registers** it with TimescaleDB so the historian stores it.

### 3. KNX engine (`src/bacnet_lab/adapters/knx/engine.py`)
- **Connect:** `xknx` over KNXnet/IP — tunnelling (to a gateway IP) or multicast routing.
- **Send:** `write_point_value` → DPT-encoded `GroupValueWrite` telegram.
- **Receive:** an inbound telegram callback decodes `GroupValueWrite`/`GroupValueResponse` by the point's DPT and updates the in-memory value. This is the "fetch live data" path.
- **Read:** `read_point_value` sends a real `GroupValueRead` (response arrives via the listener — eventually consistent) and returns the latest cached value.
- **DPT support:** 1.x boolean, 5.x 8-bit/percent, 9.x 2-byte float (with manual fallback); other types fall back to raw bytes. Encode/decode are inverses (unit-tested in `tests/unit/test_knx_decode.py`).
- **Graceful degradation:** `xknx` is lazy-imported; missing library or unreachable gateway logs and no-ops — boot never breaks.

### 4. Storage (`TimescaleDB historian`)
The historian samples all in-memory device points on a fixed grid and writes to the `point_reading` hypertable, with 1m/15m/1h continuous-aggregate rollups. Imported KNX devices are registered on import, so their live values are stored automatically.

### 5. Status surfacing
`GET /api/discovery` returns `knx_status`:
```json
{ "connected": true, "gateway": "10.0.0.5", "exposed": 40, "subscribed": 40 }
```
The frontend Discovery page shows a badge (green = connected gateway, with exposed/subscribed counts).

---

## How to go live against real KNX hardware

1. **Set environment:**
   ```
   BACNET_LAB_KNX_ENABLED=true
   BACNET_LAB_KNX_GATEWAY_IP=<KNXnet/IP gateway IP>   # empty = multicast routing
   BACNET_LAB_KNX_GATEWAY_PORT=3671
   BACNET_LAB_TSDB_ENABLED=true                       # for storage
   ```
2. **Rebuild** (installs `xknx`): `docker compose up -d --build bacnet-lab`.
3. **Import the ETS file** for the installation via the Discovery page (or `POST /api/discovery/import/ets`).
4. Verify `knx_status.connected = true` and watch values arrive in `/api/timeseries/readings`.

You need a **KNXnet/IP gateway** (IP↔TP bridge) on the network — KNX twisted-pair is not directly reachable from IP without one.

---

## Verified vs not

**Verified (this environment):**
- ETS parse → import → device created, activated, registered in TimescaleDB (point rows present).
- App boots with `xknx` installed; `knx_status` served; Discovery badge renders.
- DPT encode/decode round-trips (unit tests, 6 cases).
- Full suite: 77 unit tests pass.

**Not verified (needs hardware):**
- Live telegrams from a physical KNX device decoding into stored readings.
- `GroupValueRead` round-trip latency / gateway behaviour.

---

## Known limitations / next steps

- **No live hardware test** — the bus path is code-complete but unproven against a real gateway.
- **Read is eventually consistent** — `read_point_value` triggers a `GroupValueRead` and returns the cached value; the fresh value lands on the next inbound telegram, not synchronously.
- **One shared gateway** per engine — no per-device gateway, no reconnect/backoff yet.
- **Simulated demo KNX devices** (`config/devices/knx_*.yaml`) still run signal models; only ETS-imported devices are real-bus candidates.
- **Other protocols:** Modbus has a real `discover()`/read/write engine but is still not wired into the runtime composite; BACnet `Who-Is` discovery is not implemented. 
