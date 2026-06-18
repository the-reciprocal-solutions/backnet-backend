from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class HttpSettings:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class BacnetSettings:
    ip: str = "0.0.0.0"
    port_start: int = 47808


@dataclass
class ModbusSettings:
    host: str = "localhost"
    port: int = 5020
    unit_start: int = 1
    unit_end: int = 10


@dataclass
class AuthSettings:
    username: str = ""
    password: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)


@dataclass
class SimulationSettings:
    enabled: bool = True
    autostart: bool = True          # run continuously from boot
    tick_hz: float = 1.0            # update frequency (ticks/sec)
    speed: float = 1.0              # sim-time acceleration factor
    seed: int | None = None         # deterministic RNG (None = random)
    noise_level: float = 1.0        # global noise multiplier
    config_path: str = "config/simulation.yaml"
    # world model
    world_enabled: bool = False
    weather: str = "temperate"
    occupancy: str = "office"
    # fault injection
    faults_enabled: bool = False
    fault_rate: float = 0.0
    # generative scaling (0 = no scaling, use YAML templates as-is)
    device_count: int = 0


@dataclass
class TimescaleSettings:
    enabled: bool = False                           # dual-write opt-in
    dsn: str = "postgres://bacnet:bacnet@localhost:5432/bacnet"
    sample_interval_s: float = 5.0                  # regular historian grid
    batch_size: int = 500                           # writer flush threshold


@dataclass
class ForecastSettings:
    # Periodic background forecasting — keeps the `forecast` table fresh so the
    # anomaly detector and frontend predictions never go blind. Needs TSDB.
    enabled: bool = True
    interval_s: float = 300.0        # re-forecast every 5 min
    resolution: str = "1m"
    horizon: int = 15                # steps ahead
    lookback_s: int = 3600           # history window fed to the model
    concurrency: int = 4             # points forecast in parallel
    max_points: int = 0              # 0 = all analog points
    analog_only: bool = True         # skip binary/multistate points


@dataclass
class LLMSettings:
    enabled: bool = False
    base_url: str = "https://ollamallm.tools.thefusionapps.com"
    api_key: str = ""
    model: str = "llama3.1:8b"
    timeout_s: float = 60.0


@dataclass
class AppSettings:
    http: HttpSettings = field(default_factory=HttpSettings)
    bacnet: BacnetSettings = field(default_factory=BacnetSettings)
    modbus: ModbusSettings = field(default_factory=ModbusSettings) # <--- ADD THIS LINE
    auth: AuthSettings = field(default_factory=AuthSettings)
    simulation: SimulationSettings = field(default_factory=SimulationSettings)
    timescale: TimescaleSettings = field(default_factory=TimescaleSettings)
    forecast: ForecastSettings = field(default_factory=ForecastSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    db_path: str = "bacnet_lab.db"
    log_level: str = "INFO"
    devices_dir: str = "config/devices"


def load_settings(config_path: str = "config/settings.yaml") -> AppSettings:
    settings = AppSettings()
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if "http" in data:
            settings.http = HttpSettings(**data["http"])
        if "bacnet" in data:
            settings.bacnet = BacnetSettings(**data["bacnet"])
        if "modbus" in data:
            settings.modbus = ModbusSettings(**data["modbus"])
        if "db_path" in data:
            settings.db_path = data["db_path"]
        if "log_level" in data:
            settings.log_level = data["log_level"]
        if "devices_dir" in data:
            settings.devices_dir = data["devices_dir"]
        if "simulation" in data:
            settings.simulation = SimulationSettings(**data["simulation"])
        if "timescale" in data:
            settings.timescale = TimescaleSettings(**data["timescale"])
        if "forecast" in data:
            settings.forecast = ForecastSettings(**data["forecast"])
        if "llm" in data:
            settings.llm = LLMSettings(**data["llm"])

    import os

    def _bool(name: str, current: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return current
        return raw.strip().lower() in ("1", "true", "yes", "on")

    settings.http.host = os.getenv("BACNET_LAB_HTTP_HOST", settings.http.host)
    settings.http.port = int(os.getenv("BACNET_LAB_HTTP_PORT", str(settings.http.port)))
    settings.bacnet.ip = os.getenv("BACNET_LAB_BACNET_IP", settings.bacnet.ip)
    settings.bacnet.port_start = int(
        os.getenv("BACNET_LAB_BACNET_PORT_START", str(settings.bacnet.port_start))
    )
    settings.db_path = os.getenv("BACNET_LAB_DB_PATH", settings.db_path)
    settings.log_level = os.getenv("BACNET_LAB_LOG_LEVEL", settings.log_level)
    settings.auth.username = os.getenv("BACNET_LAB_AUTH_USERNAME", settings.auth.username)
    settings.auth.password = os.getenv("BACNET_LAB_AUTH_PASSWORD", settings.auth.password)
    settings.modbus.host = os.getenv("BACNET_LAB_MODBUS_HOST", settings.modbus.host)
    settings.modbus.port = int(os.getenv("BACNET_LAB_MODBUS_PORT", str(settings.modbus.port)))
    settings.modbus.unit_start = int(
        os.getenv("BACNET_LAB_MODBUS_UNIT_START", str(settings.modbus.unit_start))
    )
    settings.modbus.unit_end = int(
        os.getenv("BACNET_LAB_MODBUS_UNIT_END", str(settings.modbus.unit_end))
    )
    sim = settings.simulation
    sim.enabled = _bool("BACNET_LAB_SIM_ENABLED", sim.enabled)
    sim.autostart = _bool("BACNET_LAB_SIM_AUTOSTART", sim.autostart)
    sim.tick_hz = float(os.getenv("BACNET_LAB_SIM_TICK_HZ", str(sim.tick_hz)))
    sim.speed = float(os.getenv("BACNET_LAB_SIM_SPEED", str(sim.speed)))
    seed_raw = os.getenv("BACNET_LAB_SIM_SEED")
    if seed_raw is not None and seed_raw.strip() != "":
        sim.seed = int(seed_raw)
    sim.noise_level = float(os.getenv("BACNET_LAB_SIM_NOISE_LEVEL", str(sim.noise_level)))
    sim.config_path = os.getenv("BACNET_LAB_SIM_CONFIG", sim.config_path)
    sim.world_enabled = _bool("BACNET_LAB_SIM_WORLD_ENABLED", sim.world_enabled)
    sim.weather = os.getenv("BACNET_LAB_SIM_WEATHER", sim.weather)
    sim.occupancy = os.getenv("BACNET_LAB_SIM_OCCUPANCY", sim.occupancy)
    sim.faults_enabled = _bool("BACNET_LAB_SIM_FAULTS_ENABLED", sim.faults_enabled)
    sim.fault_rate = float(os.getenv("BACNET_LAB_SIM_FAULT_RATE", str(sim.fault_rate)))
    sim.device_count = int(os.getenv("BACNET_LAB_SIM_DEVICE_COUNT", str(sim.device_count)))

    ts = settings.timescale
    ts.enabled = _bool("BACNET_LAB_TSDB_ENABLED", ts.enabled)
    ts.dsn = os.getenv("BACNET_LAB_TSDB_DSN", ts.dsn)
    ts.sample_interval_s = float(
        os.getenv("BACNET_LAB_TSDB_SAMPLE_INTERVAL_S", str(ts.sample_interval_s))
    )
    ts.batch_size = int(os.getenv("BACNET_LAB_TSDB_BATCH_SIZE", str(ts.batch_size)))

    fc = settings.forecast
    fc.enabled = _bool("BACNET_LAB_FORECAST_ENABLED", fc.enabled)
    fc.interval_s = float(os.getenv("BACNET_LAB_FORECAST_INTERVAL_S", str(fc.interval_s)))
    fc.resolution = os.getenv("BACNET_LAB_FORECAST_RESOLUTION", fc.resolution)
    fc.horizon = int(os.getenv("BACNET_LAB_FORECAST_HORIZON", str(fc.horizon)))
    fc.lookback_s = int(os.getenv("BACNET_LAB_FORECAST_LOOKBACK_S", str(fc.lookback_s)))
    fc.concurrency = int(os.getenv("BACNET_LAB_FORECAST_CONCURRENCY", str(fc.concurrency)))
    fc.max_points = int(os.getenv("BACNET_LAB_FORECAST_MAX_POINTS", str(fc.max_points)))
    fc.analog_only = _bool("BACNET_LAB_FORECAST_ANALOG_ONLY", fc.analog_only)

    llm = settings.llm
    llm.enabled = _bool("BACNET_LAB_LLM_ENABLED", llm.enabled)
    llm.base_url = os.getenv("BACNET_LAB_LLM_BASE_URL", llm.base_url)
    llm.api_key = os.getenv("BACNET_LAB_LLM_API_KEY", llm.api_key)
    llm.model = os.getenv("BACNET_LAB_LLM_MODEL", llm.model)
    llm.timeout_s = float(os.getenv("BACNET_LAB_LLM_TIMEOUT_S", str(llm.timeout_s)))

    return settings
