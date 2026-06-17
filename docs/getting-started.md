# Getting Started

BACnet Lab is a real-time BACnet/IP simulator with a time-series historian (TimescaleDB) and zero-shot forecasting (Chronos). One `docker compose up` gives you live BACnet devices, a REST/SSE API, a web dashboard, persisted history, and forecasts.

## Prerequisites

- Docker + Docker Compose (Linux recommended — `network_mode: host` is required for BACnet UDP broadcast).
- ~3 GB free disk if you bake the Chronos model into the image (CPU torch is heavy).

## 1. Configure

```bash
cp .env.example .env
# edit .env — at minimum set the auth password
```

Key env vars (full list in `.env.example`):

| Var | Default | Purpose |
|-----|---------|---------|
| `BACNET_LAB_AUTH_USERNAME/PASSWORD` | admin / change-me | HTTP Basic Auth |
| `BACNET_LAB_SIM_AUTOSTART` | true | run the simulation from boot |
| `BACNET_LAB_SIM_SPEED` | 60 | sim-time acceleration (60 = 24h in 24min; 1 = real-time) |
| `BACNET_LAB_SIM_WORLD_ENABLED` | true | coupled building physics |
| `BACNET_LAB_SIM_DEVICE_COUNT` | 0 | replicate templates to N devices (0 = as-is) |
| `BACNET_LAB_TSDB_ENABLED` | true | dual-write history to TimescaleDB |
| `BACNET_LAB_TSDB_DSN` | …@localhost:5544/bacnet | TimescaleDB connection |
| `BACNET_LAB_TSDB_SAMPLE_INTERVAL_S` | 5 | regular-grid sampling period |

## 2. Start

```bash
docker compose up -d --build
```

This launches **two** services:
- `timescaledb` — TimescaleDB on host port **5544** (5432 is often taken by a host Postgres).
- `bacnet-lab` — the simulator + API.

Wait ~20s for TimescaleDB to initialize on first boot. Check it's up:

```bash
curl -u admin:admin123 http://localhost:8080/api/health
# {"status":"ok","version":"0.1.0","devices_count":8,"active_scenarios":0}
```

Open the dashboard: **http://localhost:8080/ui** (or `http://localhost:8080` → redirects to `/ui`). Login with your `.env` credentials.

## 3. See live data

Values move continuously on boot — no manual start needed.

```bash
# all current values (one flat list)
curl -u admin:admin123 http://localhost:8080/api/simulation/snapshot

# live stream (Server-Sent Events, ~1s)
curl -N -u admin:admin123 http://localhost:8080/api/simulation/stream

# one device's points
curl -u admin:admin123 http://localhost:8080/api/devices/1001
```

Real **BACnet clients** (BMS/SCADA) read live `presentValue`s directly over BACnet/IP — one device per UDP port from 47808.

## 4. History (TimescaleDB)

Every point is sampled on a 5s grid into TimescaleDB with 1m/15m/1h rollups.

```bash
# 1-minute aggregated history
curl -u admin:admin123 "http://localhost:8080/api/history/AHU-01/SupplyAirTemp?res=1m"

# latest value of every point, pivoted per device
curl -u admin:admin123 http://localhost:8080/api/history/devices/latest

# raw inspection
docker compose exec timescaledb psql -U bacnet -d bacnet \
  -c "SELECT count(*) FROM point_reading;"
```

## 5. Forecasting (Chronos)

```bash
# model availability
curl -u admin:admin123 http://localhost:8080/api/forecast/info

# forecast 6 steps ahead (p10/p50/p90)
curl -u admin:admin123 "http://localhost:8080/api/forecast/AHU-01/SupplyAirTemp?res=1m&horizon=6"
```

Out of the box the forecast uses a **naive** fallback. The default Dockerfile bakes in CPU **torch + chronos-forecasting**, so `model` shows `amazon/chronos-bolt-small`. To run lean (naive only), comment out the ML stage in the `Dockerfile` and rebuild.

Verify the model end-to-end against real DB data (accuracy vs persistence baseline + interval coverage):

```bash
docker compose exec -T bacnet-lab python - < scripts/verify_forecast.py
# model_ran : amazon/chronos-bolt-small
# MAE (p50) : 0.023   persistence MAE : 0.067   → model beats baseline
# VERDICT   : PASS
```

## 6. Local development (no Docker)

```bash
pip install -e ".[dev]"          # core deps
pip install -r requirements-ml.txt  # optional: real Chronos
# point at a TimescaleDB you run yourself:
export BACNET_LAB_TSDB_DSN=postgres://bacnet:bacnet@localhost:5432/bacnet
python -m bacnet_lab
```

## Common issues

| Symptom | Cause / fix |
|---------|-------------|
| `timescaledb` container restarting, `Address in use` | Host already runs Postgres on 5432. We publish 5544 instead — keep `…@localhost:5544` in the DSN. |
| Login popup keeps rejecting | Browser cached old Basic Auth creds. Use a fresh/incognito window. |
| `model: naive` in forecasts | torch/chronos not installed — bake the ML stage in the Dockerfile, or `pip install -r requirements-ml.txt`. |
| History empty | `BACNET_LAB_TSDB_ENABLED` is false, or TimescaleDB not reachable at the DSN. Check `docker compose logs bacnet-lab | grep -i timescale`. |
| BACnet clients can't discover devices | `network_mode: host` only works on Linux; broadcast is blocked in bridged Docker on macOS/Windows. |

## Where next

- [api.md](api.md) — full REST/SSE/metrics/history/forecast reference.
- [architecture.md](architecture.md) — system design, diagrams, the simulation engine and the TimescaleDB → Chronos → copilot pipeline.
