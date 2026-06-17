# BACnet Network Simulator

**Open-source BACnet/IP simulator with a real-time data engine, a TimescaleDB historian, and zero-shot forecasting.** 8 virtual HVAC + power devices, REST/SSE API, web dashboard, and a one-command Docker stack. No physical hardware.

Built for developers building BMS integrations, SCADA connectors, building analytics, or ML on building data who need a realistic, *moving* BACnet network — and a time-series + forecasting pipeline — running in seconds.

---

## Why

Testing BACnet integrations usually means expensive hardware and static demo data. This simulator gives you a fully functional BACnet/IP network on your dev machine that **behaves**:

- **Live by default** — every point moves on boot from per-point signal models (no manual start).
- **Physically correlated** — a coupled world model links occupancy → CO₂ → zone temp ↔ HVAC actuators.
- **Persisted** — every reading is sampled on a regular grid into **TimescaleDB** with 1m/15m/1h rollups.
- **Predictive** — **Chronos** zero-shot forecasts (p10/p50/p90) per point, served over REST.
- **Observable** — web dashboard, SSE live stream, Prometheus `/metrics`, HMAC webhooks.

---

## Features

| Feature | Description |
|---------|-------------|
| **8 Virtual BACnet/IP Devices** | AHU, 2 FCUs, thermostat, zone controller, outdoor temp & CO₂ sensors, 3-phase power meter |
| **~55 BACnet Points** | Analog in/out/value, binary I/O, multi-state |
| **Real-time simulation engine** | Always-on, env-configurable signal models: `sine`, `random_walk`, `pid_actuator`, `first_order_lag`, `derived`, `step`, `multistate_cycle`, … |
| **Coupled world physics** | Outdoor temp + occupancy + valves drive zone temp/CO₂/humidity |
| **TimescaleDB historian** | Regular-grid sampling + continuous aggregates + retention/compression |
| **Chronos forecasting** | Zero-shot probabilistic forecasts; naive fallback when torch absent |
| **REST + SSE API** | Devices, scenarios, webhooks, events, simulation control, live stream, history, forecast |
| **Web Dashboard** | HTMX auto-refresh, zero JS build |
| **Fault injection + scaling** | Inject stuck/spike/drift/offline; replicate to N devices via one env var |
| **Docker Compose** | Simulator + TimescaleDB, one command, `network_mode: host` |
| **HTTP Basic Auth** | Optional, via environment variables |

---

## Quick Start

```bash
git clone <repo-url> bacnet-simulator
cd bacnet-simulator
cp .env.example .env          # set your auth password
docker compose up -d --build  # starts the simulator + TimescaleDB
```

Wait ~20s for TimescaleDB on first boot, then:

```bash
curl -u admin:admin123 http://localhost:8080/api/health
# {"status":"ok","version":"0.1.0","devices_count":8,"active_scenarios":0}
```

- **Dashboard:** http://localhost:8080/ui (login from `.env`)
- **Live values:** `GET /api/simulation/snapshot` · **stream:** `GET /api/simulation/stream` (SSE)
- **History:** `GET /api/history/AHU-01/SupplyAirTemp?res=1m`
- **Forecast:** `GET /api/forecast/AHU-01/SupplyAirTemp?horizon=6`

> `network_mode: host` (BACnet UDP broadcast) is Linux-only. TimescaleDB is published on host port **5544** (5432 is usually taken by a host Postgres).

Full walkthrough: **[docs/getting-started.md](docs/getting-started.md)**.

---

## Simulated Devices

| Device | ID | Points | Description |
|--------|----|--------|-------------|
| AHU-01 | 1001 | 12 | Air Handling Unit — supply/return/mixed temps, valves, fans, pressure |
| FCU-01 | 2001 | 7 | Fan Coil Unit Zone 1 — room temp, valve, fan |
| FCU-02 | 2002 | 7 | Fan Coil Unit Zone 2 |
| TSTAT-01 | 3001 | 6 | Thermostat — temp, setpoints, occupancy |
| ZC-01 | 4001 | 7 | Zone Controller — damper, airflow, CO₂, occupancy |
| OAT-01 | 5001 | 2 | Outdoor Temperature Sensor |
| CO2-01 | 5002 | 3 | CO₂ Sensor |
| PM-01 | 6001 | 11 | 3-phase Power Meter — kW, kVA, V/A per phase, PF, Hz, kWh |

Each device runs on its own UDP port (from 47808) and is BACnet-discoverable. Devices are YAML in `config/devices/`; each point can declare a `simulation:` block selecting its signal model. Set `BACNET_LAB_SIM_DEVICE_COUNT=N` to replicate templates to N independent devices.

---

## The Data Pipeline

```
Simulation engine ─▶ DeviceService ─▶ BACnet/IP presentValue ─▶ BACnet clients (BMS/SCADA)
                          │
                          ├─▶ Event bus ─▶ HMAC webhooks
                          └─▶ Historian ─▶ TimescaleDB ─▶ Chronos forecast ─▶ /api/forecast
                                                       └─▶ /api/history · dashboard · SSE
```

The simulation engine writes through the same path real devices would, so the BACnet wire, events, history, and forecasting all work whether data is simulated or real.

---

## Forecasting

Out of the box the Docker image bakes in CPU **torch + chronos-forecasting**, so `/api/forecast/{point}` returns real `amazon/chronos-bolt-small` predictions. Without torch it falls back to a naive persistence+drift model (same response shape). Verify end-to-end against real DB data:

```bash
docker compose exec -T bacnet-lab python - < scripts/verify_forecast.py
# model_ran : amazon/chronos-bolt-small
# MAE (p50) : 0.023   persistence MAE : 0.067   → beats baseline
# VERDICT   : PASS
```

To run lean (naive only): comment out the ML stage in the `Dockerfile` and rebuild.

---

## REST API (summary)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/devices` · `/api/devices/{id}` | List / device detail |
| PUT | `/api/devices/{id}/points` | Write a point value |
| GET/POST | `/api/scenarios` · `/api/scenarios/{id}/start\|stop` | Scenarios |
| GET | `/api/simulation/status\|generators\|snapshot` | Engine introspection |
| GET | `/api/simulation/stream` | **SSE** live snapshot stream |
| POST | `/api/simulation/start\|stop` · `/faults` | Engine + fault control |
| GET | `/api/history/{point}` · `/api/history/devices/latest` | TimescaleDB history + pivot |
| GET | `/api/forecast/{point}` · `/api/forecast/info` | Forecasts |
| GET/POST/DELETE | `/api/endpoints…` | Webhook endpoints |
| GET | `/api/events` · `/api/alarms` | Event + alarm log |
| GET | `/metrics` | Prometheus metrics |

Full reference: **[docs/api.md](docs/api.md)**.

---

## Configuration

Everything is env-driven (see `.env.example`). Highlights:

| Var | Default | Purpose |
|-----|---------|---------|
| `BACNET_LAB_SIM_AUTOSTART` | true | run the engine from boot |
| `BACNET_LAB_SIM_SPEED` | 60 | sim-time acceleration (60 = 24h in 24min; 1 = real-time) |
| `BACNET_LAB_SIM_WORLD_ENABLED` | true | coupled building physics |
| `BACNET_LAB_SIM_DEVICE_COUNT` | 0 | replicate templates to N devices |
| `BACNET_LAB_SIM_FAULTS_ENABLED` | false | random fault injection |
| `BACNET_LAB_TSDB_ENABLED` | true | dual-write history to TimescaleDB |
| `BACNET_LAB_TSDB_SAMPLE_INTERVAL_S` | 5 | regular-grid sampling period |
| `BACNET_LAB_AUTH_USERNAME/PASSWORD` | admin / change-me | HTTP Basic Auth |

---

## Local Development

```bash
pip install -e ".[dev]"             # core
pip install -r requirements-ml.txt  # optional: real Chronos
export BACNET_LAB_TSDB_DSN=postgres://bacnet:bacnet@localhost:5432/bacnet
python -m bacnet_lab
```

---

## Tech Stack

- **Python 3.11+**, async throughout
- **BAC0 / BACpypes3** — BACnet/IP stack
- **FastAPI** + Uvicorn — REST + SSE
- **TimescaleDB** (asyncpg) — time-series historian; **SQLite** — metadata/events
- **Chronos** (`chronos-forecasting` + torch) — zero-shot forecasting
- **HTMX** + Jinja2 + Pico CSS — dashboard

Hexagonal architecture (domain → ports → application → adapters). See **[docs/architecture.md](docs/architecture.md)**.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Getting Started](docs/getting-started.md) | Setup, start, history, forecasting, troubleshooting |
| [API Reference](docs/api.md) | REST/SSE/metrics/history/forecast with examples |
| [Architecture](docs/architecture.md) | Design, diagrams, simulation engine + TimescaleDB → Chronos pipeline |

---

## License

MIT — see [LICENSE](LICENSE).
# backnet-backend
