# Discovery & Multi-Protocol Module — Production Readiness Analysis

_Analysis date: 2026-06-20 • Scope: discovery endpoint + BACnet / KNX / Modbus / MQTT adapters_

## TL;DR

| Question | Answer |
|---|---|
| Production-ready for real-time use? | **No, not as-is.** The discovery API is a simulation/inventory view, not a live network scanner. Some engines do real I/O, others are not wired in. |
| Simulation only? | The `/api/discovery` endpoint is **simulation-only**. Underlying engines are mixed: BACnet/MQTT/KNX do real I/O, Modbus is real but unwired. |
| Proper per-protocol handling? | **Partial.** BACnet: real. MQTT: real. KNX: write real / read cached. Modbus: real code but **not instantiated at runtime**. |
| KNX / Modbus data mapping ready? | **No.** Addresses + datatypes are auto-derived placeholders, not real ETS/register maps. See mapping section. |

---

## 1. What the discovery module actually does

File: [discovery.py](../src/bacnet_lab/adapters/http/routers/discovery.py)

`GET /api/discovery` does **not** scan any network. It:

1. Calls `device_service.list_devices()` — reads devices that were loaded from YAML config into the local DB.
2. Groups them by their `protocol` tag (`bacnet` / `mqtt` / `knx` / `modbus`).
3. Returns buckets + per-protocol counts + grand total.

```python
devices = await get_container().device_service.list_devices()
buckets = {p: [] for p in PROTOCOLS}
for d in devices:
    protocol = d.protocol or "bacnet"
    buckets[protocol].append({...})
```

**Implication:** this is an **inventory view of pre-configured simulated devices**, not discovery of real field devices. A device's protocol is just a string tag set in YAML ([device.py:33](../src/bacnet_lab/domain/models/device.py#L33), default `"bacnet"`).

> A real Modbus network scan **does** exist — `ModbusEngine.discover()` in [modbus/engine.py](../src/bacnet_lab/adapters/modbus/engine.py) probes unit IDs over TCP — but the `/api/discovery` endpoint never calls it. The two are unconnected.

---

## 2. Real-time readiness — per protocol

### BACnet — real
- [bacnet/engine.py](../src/bacnet_lab/adapters/bacnet/engine.py), `BAC0Engine`, real BAC0 stack.
- Primary engine in the composite; owns authoritative point state.
- Non-BACnet devices are deliberately skipped from the BAC0 stack (`protocol != "bacnet"` guard, engine.py:33) — they exist only as metadata.

### MQTT — real
- `MqttEngine`, wired in [bootstrap.py:94](../src/bacnet_lab/bootstrap.py#L94) when `settings.mqtt.enabled`.

### KNX — write real, read cached
- [knx/engine.py](../src/bacnet_lab/adapters/knx/engine.py), real `xknx` IP tunnelling/routing.
- `write_point_value` → pushes a `GroupValueWrite` telegram (real bus write).
- **`read_point_value` returns the in-memory cached value — there is no `GroupValueRead` from the bus.** So reads are simulation state, not live field reads.
- Wired in [bootstrap.py:102](../src/bacnet_lab/bootstrap.py#L102) when `settings.knx.enabled`.
- Degrades to no-op if `xknx` missing or gateway unreachable (boot never breaks).

### Modbus — real code, NOT wired
- [modbus/engine.py](../src/bacnet_lab/adapters/modbus/engine.py), `ModbusEngine`, real `pymodbus` `AsyncModbusTcpClient`. Read/write/discover all do real TCP I/O.
- **Gap: bootstrap only adds MQTT and KNX to `extra_engines`. `ModbusEngine` is never instantiated or added to the composite** ([bootstrap.py:93-109](../src/bacnet_lab/bootstrap.py#L93-L109)). At runtime Modbus is a config tag only; the engine is dead code from the app's perspective.

---

## 3. Production blockers / implementation points

1. **Discovery is not real discovery.** Endpoint groups configured devices; it never probes a network. To make it real, wire `engine.discover()` (Modbus already has it; BACnet `Who-Is`, KNX has none) behind a new scan endpoint and merge results into the device store.
2. **Modbus engine unwired.** Add `ModbusEngine` to `extra_engines` in bootstrap gated on `settings.modbus.enabled`. Without this, Modbus is non-functional at runtime.
3. **KNX reads are fake.** Implement real `GroupValueRead` round-trip if live reads are required; today reads return cached sim state.
4. **Composite read path is BACnet-only.** `CompositeDeviceNetwork.read_point_value` always reads from `_primary` (the BACnet engine) ([network_composite.py](../src/bacnet_lab/adapters/network_composite.py)). A KNX-only or Modbus-only device has no BACnet state → reads are served from simulation, not the real protocol.
5. **No real-device addressing.** Addresses are auto-derived from `device_id`/point index (see §4), not from real-world commissioning data. Two real devices cannot be addressed deterministically.
6. **Connection robustness.** Modbus uses a single shared client keyed to one `host:port` — no per-device gateway, no pooling, no reconnect/backoff beyond connect-on-demand. KNX single gateway. Fine for a lab, thin for production.
7. **Error model.** Engine failures are swallowed/logged in the composite fan-out (`_fan` catches all) — a failed real write is logged, not surfaced to the caller.

---

## 4. KNX & Modbus — data mapping requirements

This is the biggest production gap. Both protocols currently use **deterministic auto-generated addressing**, not real maps.

### Modbus mapping

Current behaviour ([modbus/engine.py](../src/bacnet_lab/adapters/modbus/engine.py)):

| Concept | Current code | Production requirement |
|---|---|---|
| Device → unit/slave ID | `unit_id = device_id` (direct 1:1) | Explicit per-device `unit_id` in config; device_id ≠ Modbus slave ID in the real world |
| Point → register address | `instance` used directly as register address | Explicit register address per point |
| Point type → register class | AI→input reg, AV/AO→holding reg, BI→discrete input, BV/BO→coil | Same mapping is reasonable, but must be declarable per point |
| Value width | reads `registers[0]` only — single 16-bit word | 32-bit / float / multi-register values need word count + word order (big/little-endian, word swap) |
| Scaling | none | raw register → engineering units (scale/offset) |
| Data type | implicit int | int16/uint16/int32/float32 etc. |

**Required to map real data:** a register map per device declaring, for each point: `unit_id`, `register_address`, `register_type` (coil / discrete / input / holding), `data_type` (uint16/int16/uint32/float32…), `word_count`, `byte/word_order`, and `scale`/`offset`. None of this exists today.

### KNX mapping

Current behaviour ([knx/engine.py](../src/bacnet_lab/adapters/knx/engine.py)):

| Concept | Current code | Production requirement |
|---|---|---|
| Point → group address | Auto-derived: `main = device_id % 32`, `middle = idx // 256`, `sub = idx % 256` | Real **group address per point** from the ETS project (commissioned values), not derived |
| Datapoint type (DPT) | Hardcoded `DPTArray(int(value) & 0xFF)` — raw byte | Correct **DPT per point**: DPT 1.001 (switch bool), DPT 5.001 (0-100%), DPT 9.x (2-byte float temp/lux), etc. Raw-byte write is wrong for most real DPTs |
| Read | Returns cached value (no bus read) | `GroupValueRead` + DPT decode for live reads |
| Address validity | `main` capped to 0-31 by modulo — collisions when device_id ≥ 32 | Unique commissioned GAs; current scheme collides |

Example config showing the gap — [knx_light_01.yaml](../config/devices/knx_light_01.yaml) defines points with units (`percent`, `luxes`, `degreesCelsius`) but **no group address and no DPT**. To talk to a real KNX device, every point needs an explicit `group_address` (e.g. `1/2/3`) and a `dpt` (e.g. `5.001`).

**Required to map real data:** per point, a real `group_address` and a `dpt`; optionally separate read/write/status GAs. The current `_group_address()` derivation must be replaced by config-driven addresses.

---

## 5. Verdict

- **Use today for:** demos, simulation, protocol-shape UI, integration testing against the simulator.
- **Not ready for:** talking to real field devices via the discovery view. Discovery doesn't scan; Modbus isn't wired; KNX/Modbus addressing and datatypes are placeholders; KNX reads are cached.
- **Smallest path to real Modbus:** wire `ModbusEngine` in bootstrap + add a per-point register map (address, type, data_type, scale).
- **Smallest path to real KNX:** add per-point `group_address` + `dpt` to config, replace `_group_address()` and the hardcoded `DPTArray`, implement `GroupValueRead`.

---

## 6. Action plan for real-time use

Ordered by dependency. P0 = blocks any real-device use, P1 = correctness for real data, P2 = hardening, P3 = scale/ops.

### Phase 0 — Wire what already exists (P0)

- [ ] **AI-1. Wire `ModbusEngine` into bootstrap.** Add `settings.modbus.enabled` flag; when on, append `ModbusEngine(host, port, unit_start, unit_end)` to `extra_engines` in [bootstrap.py:93-109](../src/bacnet_lab/bootstrap.py#L93-L109). _Without this Modbus is dead at runtime._ (S)
- [ ] **AI-2. Add `enabled` flag to `ModbusSettings`** in [config.py](../src/bacnet_lab/infrastructure/config.py) + `BACNET_LAB_MODBUS_ENABLED` env, matching MQTT/KNX pattern. (S)
- [ ] **AI-3. Fix composite read path.** `CompositeDeviceNetwork.read_point_value` always reads BACnet primary ([network_composite.py](../src/bacnet_lab/adapters/network_composite.py)). Route reads to the engine matching the device's `protocol` so KNX/Modbus-only devices read from their real bus, not sim state. (M)

### Phase 1 — Real addressing & datatypes (P0/P1)

- [ ] **AI-4. Modbus register map in config.** Add per-point fields: `unit_id`, `register_address`, `register_type` (coil/discrete/input/holding), `data_type` (uint16/int16/uint32/float32), `word_count`, `word_order`. Parse in [device_factory.py](../src/bacnet_lab/adapters/bacnet/device_factory.py). Stop using `device_id` as unit and `instance` as address. (M)
- [ ] **AI-5. Modbus multi-register + scaling.** Decode 32-bit/float across registers with endianness; apply `scale`/`offset` raw→engineering units in `read_point_value`/`write_point_value`. (M)
- [ ] **AI-6. KNX group address + DPT in config.** Add per-point `group_address` (e.g. `1/2/3`) and `dpt` (e.g. `1.001`, `5.001`, `9.x`). Replace auto-derived `_group_address()` and hardcoded `DPTArray(raw & 0xFF)` in [knx/engine.py](../src/bacnet_lab/adapters/knx/engine.py) with real DPT encode per point. (M)
- [ ] **AI-7. KNX real reads.** Implement `GroupValueRead` round-trip + DPT decode in `read_point_value` instead of returning cached value. (M)

### Phase 2 — Real discovery (P1)

- [ ] **AI-8. Scan endpoint.** New `POST /api/discovery/scan?protocol=` that calls live `engine.discover()` and merges found devices into the store — separate from the existing inventory `GET /api/discovery`. (M)
- [ ] **AI-9. BACnet + KNX discovery.** BACnet `Who-Is`/`I-Am` enumeration; KNX has no native discovery → document as config-only or ETS-import. Modbus `discover()` already exists. (M/L)
- [ ] **AI-10. ETS / register-map import.** Optional: import KNX group addresses from ETS export and Modbus maps from CSV to avoid hand-writing YAML. (L)

### Phase 3 — Hardening & ops (P2/P3)

- [ ] **AI-11. Surface engine errors.** Composite `_fan` swallows write failures ([network_composite.py](../src/bacnet_lab/adapters/network_composite.py)) — propagate real-device write failures to callers, not just logs. (S)
- [ ] **AI-12. Connection resilience.** Per-device gateway/host support, reconnect + backoff for Modbus/KNX clients (today single shared client, connect-on-demand). (M)
- [ ] **AI-13. Address-collision guard.** Current KNX `device_id % 32` collides for device_id ≥ 32 — once real GAs land (AI-6) add a uniqueness validator at load. (S)
- [ ] **AI-14. Per-protocol health + timeouts.** Read/write timeouts, per-engine health in the discovery/status view, metrics per protocol. (M)
- [ ] **AI-15. Integration tests against real stacks.** Modbus TCP server fixture, KNX gateway/sim, BACnet device — verify real read/write/scan paths end-to-end. (M)

_Effort: S = <½ day, M = 1-3 days, L = >3 days._

### Critical path to first real device

`AI-2 → AI-1 → AI-4 → AI-5` for Modbus, or `AI-6 → AI-7` for KNX, plus `AI-3` for both. Everything else is correctness/hardening on top.
