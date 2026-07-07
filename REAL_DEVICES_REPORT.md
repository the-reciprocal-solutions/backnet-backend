# Real BACnet Device Integration — Work Report

**Date:** 2026-07-04
**Site:** Client location (live BMS network)
**Objective:** Connect the backend to the physical BACnet controllers on the client network and pull their live point data, instead of using the built-in simulator.

---

## 1. Summary

The system now reads **12 live DDC controllers** (**96 points**) directly off the client's building-management network and serves that data through the existing REST API and dashboard. Values refresh every 15 seconds.

The controllers are **Distech Controls** DDCs (models ECY-S1000 / ECY-303) on a **KNX-to-BACnet-converted** site — the field devices are KNX, exposed to the network as BACnet/IP by the controllers.

---

## 2. Protocol & Framework Used

| Layer | Technology |
|-------|-----------|
| Field protocol | **BACnet/IP** over **UDP port 47808** |
| BACnet stack | **BAC0 2025.09.15** (Python library, built on **bacpypes3**) |
| Backend | **Python 3.10** + **FastAPI** (REST + SSE API) |
| Time-series store | **TimescaleDB** (runs in Docker, host port 5544) |
| Frontend | **React + Vite** dev server (`:3003`), proxies API calls to the backend |
| Role | Backend acts as a **BACnet client** — it *reads* the controllers; it does not expose or command them |

**How BACnet is spoken here:** the backend opens a single BACnet/IP endpoint bound to UDP 47808, sends **ReadProperty** requests to each controller's objects, and reads the `presentValue` of every point on a timed loop. Each reading is pushed into the app's event pipeline, stored in the historian, and served over `/api/devices`.

---

## 3. How We Found the Devices

Standard BACnet discovery did **not** work on this site. The path to a working device list:

1. **Confirmed network reachability.** The laptop had to be on the BMS subnet `192.168.20.0/23` (via Ethernet `192.168.21.118`). From an unrelated Wi-Fi network the controllers were unreachable — an IP-level `ping` to a controller was the first checkpoint.

2. **Broadcast Who-Is → nothing.** The normal BACnet way to find devices is a **Who-Is broadcast**; every device answers **I-Am**. Here it returned **zero** — both the subnet broadcast and a global broadcast. These controllers simply do not answer Who-Is.

3. **Unicast Who-Is → still nothing.** Sending Who-Is directly to each host also returned nothing. The controllers ignore Who-Is entirely.

4. **Discovered the port quirk.** A `ReadProperty` sent from a random UDP port got no reply, but the same read sent from **source port 47808** succeeded. These controllers only answer a client bound to 47808.

5. **Enumerated by wildcard device read.** Because Who-Is is dead, devices were found a different way: reading the **wildcard device instance `4194303`** (`objectIdentifier` + `objectName`). A controller responds to that with its **real device ID and name**. Running this read against every live host on the subnet returned the full device list.

6. **Found the live hosts first.** To know which addresses to probe, a fast parallel **ping sweep** of the whole `/23` (≈130 live hosts) narrowed the field; the wildcard read was then run against those hosts.

**Net method that works on this site:** ping-sweep the subnet → wildcard-read `device 4194303` on each live host → list the responding controllers by IP → read each controller's object list and point values by unicast ReadProperty from port 47808.

---

## 4. Key Findings (why the standard approach failed)

- **Controllers reply only to source UDP port 47808** — a client on a random port gets silence.
- **They do not answer Who-Is** (broadcast or unicast) — only `ReadProperty`. Auto-discovery tools that rely on Who-Is find nothing here.
- **They reject ReadPropertyMultiple** — properties must be read one at a time.
- **Correct subnet mask matters** — the network is `/23`; using `/24` reaches only half the addresses.
- **Docker on Windows cannot do BACnet** — its host-network mode binds a virtual machine's network, not the laptop's real NIC, so a containerised backend never reaches the controllers. The backend must run natively on Windows; only the database stays in Docker.

---

## 5. Device Inventory (enumerated live)

| Device | Device ID | IP | Model | Points | Status |
|--------|-----------|----|-------|-------:|--------|
| DDC1 | 1101 | 192.168.20.211 | ECY-S1000 | 11 | online |
| DDC2 | 1102 | 192.168.20.212 | — | 7 | online |
| DDC3 | 1232 | 192.168.20.213 | — | 24 | online |
| DDC4 | 1227 | 192.168.20.214 | — | 8 | online |
| DDC5 | 1105 | 192.168.20.215 | — | 9 | online |
| DDC8 | 1108 | 192.168.20.218 | — | 8 | online |
| DDC13 | 1113 | 192.168.20.223 | — | 2 | online |
| DDC-3 | 20231 | 192.168.20.231 | — | 8 | online |
| DDC-9_10 | 20232 | 192.168.20.232 | — | 3 | online |
| DDC-5 | 20233 | 192.168.20.233 | — | 8 | online |
| DDC-13 | 20234 | 192.168.20.234 | — | 2 | online |
| DDC6_303_TERRACE | 20235 | 192.168.20.235 | ECY-303 | 6 | online |
| FIN_Connector | 2972162 | 192.168.21.64 | Niagara supervisor | — | not onboarded |

**Total: 12 DDC controllers online, 96 live points.**

Example live data (DDC3 — BioPond circulation pumps): `BioPond CP Pump-1..12 Run Status`, each reading live on/off.

---

## 6. Current State

- Backend runs natively on the laptop, serving `http://localhost:8080`; the frontend at `http://localhost:3003` shows the devices (login `admin` / `admin123`).
- All 12 DDCs report `online` with live point values, refreshing every 15 seconds.
- Data is also written to the TimescaleDB historian (Docker, port 5544) for history/trends.

---

## 7. Open Items / Next Steps

1. **FIN_Connector not onboarded** — a Niagara supervisor (`192.168.21.64`) with a very large object list; it timed out during onboarding. It is not a DDC; can be added later with a longer read timeout or a point whitelist.
2. **Inconsistent device naming** — two blocks exist: a clean `DDC1..DDC13` set (`.211-.223`) and a second block (`.231-.235`) labelled `DDC-3 / DDC-5 / DDC-9_10 / DDC-13 / DDC6_303_TERRACE`, with overlapping numbers. This reflects the client's actual configuration and needs on-site reconciliation (which are primary vs. duplicate/mirrored).
3. **Missing clean labels DDC7, DDC11, DDC12** — either offline, behind the FIN supervisor, or under a different name. Re-sweep when reachable.
4. **Always-on operation** — the backend currently runs as a manual process; for production it should be a Windows service.
5. **Security** — the API is currently reachable on the LAN with default credentials; tighten before leaving it running.

---

## 8. Operating Notes

- The laptop **must** be on the BMS network (`192.168.20-21.x`); verify with a `ping` to a controller before starting.
- Run the backend **natively** on Windows (not in Docker) so it can reach the controllers; keep only the database in Docker.
- The controller list lives in a config file; add or edit controllers there and restart the backend.

---

## 9. Troubleshooting Quick Reference

| Symptom | Cause | Fix |
|---------|-------|-----|
| Device page shows "not found" | Requests hitting the Docker backend (can't reach devices on Windows) | Use the native backend |
| Controller unreachable (`ping` fails) | Laptop not on the BMS subnet | Join the BMS Ethernet/Wi-Fi (`192.168.20-21.x`) |
| Discovery finds nothing | Controllers don't answer Who-Is | Enumerate by wildcard `device 4194303` read; list controllers by IP |
| No reply from a controller | Client not bound to port 47808 | Bind the BACnet client to UDP 47808 |
| Half the network missing | Wrong subnet mask (`/24` on a `/23` net) | Use a `/23` mask |
