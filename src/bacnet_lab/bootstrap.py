from __future__ import annotations

import logging
from dataclasses import dataclass

from bacnet_lab.adapters.bacnet.device_factory import (
    generate_fleet,
    load_all_devices,
    scale_devices,
)
from bacnet_lab.adapters.bacnet.engine import BAC0Engine
from bacnet_lab.adapters.bacnet.real_poller import RealBACnetPoller
from bacnet_lab.adapters.event_bus.in_process import InProcessEventPublisher
from bacnet_lab.adapters.persistence.migrations import run_migrations
from bacnet_lab.adapters.persistence.sqlite_repos import (
    SqliteAlarmRepository,
    SqliteAssetRepository,
    SqliteDeviceRepository,
    SqliteEndpointRepository,
    SqliteEventLogRepository,
)
from bacnet_lab.adapters.scenarios.alarm import AlarmScenario
from bacnet_lab.adapters.scenarios.device_offline import DeviceOfflineScenario
from bacnet_lab.adapters.scenarios.hvac_day_cycle import HvacDayCycleScenario
from bacnet_lab.adapters.scenarios.manual_override import ManualOverrideScenario
from bacnet_lab.adapters.scenarios.predictive_validation import (
    AhuVibrationScenario,
    CompressorShortCycleScenario,
    CoolingInefficiencyScenario,
    SensorStuckScenario,
)
from bacnet_lab.adapters.scenarios.registry import ScenarioRegistry
from bacnet_lab.adapters.web.websocket import ConnectionManager, WsBroadcaster
from bacnet_lab.adapters.webhook.delivery import WebhookDeliveryAdapter
from bacnet_lab.adapters.webhook.subscriber import WebhookSubscriber
from bacnet_lab.application.alarm_service import AlarmService
from bacnet_lab.application.anomaly_feed import AnomalyFeed
from bacnet_lab.application.asset_service import AssetService
from bacnet_lab.application.device_service import DeviceService
from bacnet_lab.adapters.persistence.timescale import TimescaleTimeSeries
from bacnet_lab.application.endpoint_service import EndpointService
from bacnet_lab.application.event_service import EventService
from bacnet_lab.application.historian_service import HistorianService
from bacnet_lab.application.pipeline_service import PipelineService
from bacnet_lab.application.prediction_service import PredictionService
from bacnet_lab.application.scenario_service import ScenarioService
from bacnet_lab.application.simulation_service import SimulationEngine
from bacnet_lab.application.telemetry_service import TelemetryService
from bacnet_lab.application.forecast_scheduler import ForecastScheduler
from bacnet_lab.copilot import CopilotService
from bacnet_lab.forecasting import ForecastService
from bacnet_lab.forecasting.anomaly_detector import AnomalyDetector
from bacnet_lab.infrastructure.config import AppSettings

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: AppSettings
    device_service: DeviceService
    scenario_service: ScenarioService
    endpoint_service: EndpointService
    event_service: EventService
    telemetry_service: TelemetryService
    simulation_engine: SimulationEngine
    alarm_service: AlarmService
    historian_service: HistorianService
    tsdb: TimescaleTimeSeries
    forecast_service: ForecastService
    forecast_scheduler: ForecastScheduler
    anomaly_detector: AnomalyDetector
    copilot_service: CopilotService
    asset_service: AssetService
    prediction_service: PredictionService
    pipeline_service: PipelineService
    anomaly_feed: AnomalyFeed
    ws_manager: ConnectionManager
    alarm_repo: SqliteAlarmRepository
    engine: BAC0Engine
    event_publisher: InProcessEventPublisher
    real_poller: RealBACnetPoller
    knx_engine: object = None  # KnxEngine instance when KNX enabled, else None
    discovery_service: object = None  # DiscoveryService (per-protocol live discovery)


async def create_container(settings: AppSettings) -> Container:
    # Run DB migrations
    await run_migrations(settings.db_path)

    # Real-device client mode: we poll physical controllers as a BACnet client.
    # Many controllers reply only to source UDP 47808, so the poller must bind
    # it. Move the simulator's exposed-device port range out of the way so the
    # poller and the sim engine never fight over the same socket.
    if settings.real.enabled and settings.real.bind_port == settings.bacnet.port_start:
        shifted = settings.bacnet.port_start + 1000
        logger.info(
            "Real poller owns UDP %d; shifting simulator BACnet port_start %d -> %d",
            settings.real.bind_port, settings.bacnet.port_start, shifted,
        )
        settings.bacnet.port_start = shifted

    # Adapters
    engine = BAC0Engine(ip=settings.bacnet.ip)

    # Multi-protocol exposure: wrap BACnet with MQTT/KNX engines when enabled.
    # BACnet stays primary (authoritative point state); others mirror writes.
    network: object = engine
    knx_engine = None
    extra_engines = []
    if settings.mqtt.enabled:
        from bacnet_lab.adapters.mqtt.engine import MqttEngine
        extra_engines.append(MqttEngine(
            host=settings.mqtt.host, port=settings.mqtt.port,
            prefix=settings.mqtt.prefix,
            username=settings.mqtt.username, password=settings.mqtt.password,
        ))
        logger.info("MQTT protocol engine enabled (%s:%d)", settings.mqtt.host, settings.mqtt.port)
    if settings.knx.enabled:
        from bacnet_lab.adapters.knx.engine import KnxEngine
        knx_engine = KnxEngine(
            gateway_ip=settings.knx.gateway_ip, gateway_port=settings.knx.gateway_port,
        )
        extra_engines.append(knx_engine)
        logger.info("KNX protocol engine enabled (gateway=%s)",
                    settings.knx.gateway_ip or "multicast-routing")
    if extra_engines:
        from bacnet_lab.adapters.network_composite import CompositeDeviceNetwork
        network = CompositeDeviceNetwork([engine, *extra_engines])

    event_publisher = InProcessEventPublisher()
    webhook_delivery = WebhookDeliveryAdapter()

    # Repositories
    device_repo = SqliteDeviceRepository(settings.db_path)
    endpoint_repo = SqliteEndpointRepository(settings.db_path)
    event_log_repo = SqliteEventLogRepository(settings.db_path)
    alarm_repo = SqliteAlarmRepository(settings.db_path)
    asset_repo = SqliteAssetRepository(settings.db_path)

    # Application services
    device_service = DeviceService(
        device_repo=device_repo,
        network=network,
        event_publisher=event_publisher,
        bacnet_port_start=settings.bacnet.port_start,
    )

    # Scenarios
    scenario_registry = ScenarioRegistry()
    scenario_registry.register(HvacDayCycleScenario(device_service, event_publisher))
    scenario_registry.register(AlarmScenario(device_service, event_publisher))
    scenario_registry.register(DeviceOfflineScenario(device_service, event_publisher))
    scenario_registry.register(ManualOverrideScenario(device_service, event_publisher))
    # Predictive-layer validation scenarios (inject known faults to confirm
    # anomaly detection + prediction + reasoning fire correctly).
    scenario_registry.register(AhuVibrationScenario(device_service, event_publisher))
    scenario_registry.register(CoolingInefficiencyScenario(device_service, event_publisher))
    scenario_registry.register(SensorStuckScenario(device_service, event_publisher))
    scenario_registry.register(CompressorShortCycleScenario(device_service, event_publisher))

    scenario_service = ScenarioService(runner=scenario_registry)

    endpoint_service = EndpointService(
        repo=endpoint_repo,
        delivery=webhook_delivery,
    )

    event_service = EventService(
        event_publisher=event_publisher,
        event_log_repo=event_log_repo,
        endpoint_repo=endpoint_repo,
        delivery=webhook_delivery,
    )

    alarm_service = AlarmService(
        event_publisher=event_publisher,
        alarm_repo=alarm_repo,
    )

    telemetry_service = TelemetryService(event_publisher=event_publisher)
    telemetry_service.set_device_service(device_service)

    # Load and initialize devices.
    # Two independent, OFF-by-default scaling paths (both no-op when 0):
    #   * fleet_size  -> multi-protocol varied fleet (task B5). Preferred for
    #     large 100+ fleets; most devices are non-bacnet so boot stays light.
    #   * device_count -> legacy single-protocol clone scaling.
    # fleet_size takes precedence when both are set.
    devices = load_all_devices(settings.devices_dir)
    if settings.simulation.fleet_size > 0:
        devices = generate_fleet(devices, settings.simulation.fleet_size)
    else:
        devices = scale_devices(devices, settings.simulation.device_count)
    logger.info("Device count after scaling: %d", len(devices))
    await device_service.initialize_devices(devices)

    # Asset registry (seeded from devices on first run)
    asset_service = AssetService(repo=asset_repo)
    await asset_service.seed_from_devices(device_service)

    # Real-time simulation engine (always-on signal generation)
    simulation_engine = SimulationEngine(device_service, settings.simulation)

    # Time-series historian (dual-write to TimescaleDB; opt-in, fail-safe)
    tsdb = TimescaleTimeSeries(settings.timescale.dsn)
    historian_service = HistorianService(
        tsdb=tsdb,
        device_service=device_service,
        event_publisher=event_publisher,
        settings=settings.timescale,
    )

    # Real BACnet device poller (client): reads physical controllers listed in
    # settings.real.config_path and ingests their live values through the same
    # event -> historian -> API pipeline. No-op unless settings.real.enabled.
    # Constructed before discovery so the BACnet discovery adapter can share its
    # single UDP-47808 client (these controllers reply only to source port 47808,
    # so poller and discovery must not fight over that socket).
    real_poller = RealBACnetPoller(
        device_service=device_service,
        tsdb=tsdb,
        event_publisher=event_publisher,
        settings=settings.real,
    )

    # Live per-protocol device discovery (BACnet subnet sweep, Modbus scan, MQTT
    # sweep, KNX ETS/gateway). Each adapter is registered best-effort so a
    # missing optional dependency never breaks boot. The BACnet adapter shares
    # the real poller's client and onboards added devices into its poll loop.
    from bacnet_lab.application.discovery_service import DiscoveryService
    discovery_service = DiscoveryService(
        device_service=device_service, tsdb=tsdb, real_poller=real_poller)
    for _import_path, _ctor in (
        ("bacnet_lab.adapters.bacnet.discovery", lambda m: m.BACnetDiscovery(local_ip=settings.bacnet.ip, poller=real_poller)),
        ("bacnet_lab.adapters.modbus.discovery", lambda m: m.ModbusDiscovery()),
        ("bacnet_lab.adapters.mqtt.discovery", lambda m: m.MqttDiscovery()),
        ("bacnet_lab.adapters.knx.discovery", lambda m: m.KnxDiscovery()),
    ):
        try:
            _mod = __import__(_import_path, fromlist=["*"])
            discovery_service.register(_ctor(_mod))
        except Exception as _e:  # noqa: BLE001
            logger.warning("Discovery adapter %s unavailable: %s", _import_path, _e)

    # Forecasting (Chronos + own DB access; naive fallback when torch absent)
    forecast_service = ForecastService(settings.timescale.dsn)

    # Periodic forecast refresh — keeps forecasts fresh for detector + UI
    forecast_scheduler = ForecastScheduler(
        forecast_service=forecast_service,
        device_service=device_service,
        settings=settings.forecast,
    )

    # Anomaly Detection (forecast-band breach + immediate hard-limit breach)
    anomaly_detector = AnomalyDetector(
        event_publisher=event_publisher,
        db=forecast_service.db,
        device_service=device_service,
    )

    # Reasoning copilot (Chronos + DB evidence + grounded LLM)
    copilot_service = CopilotService(forecast_service, settings.llm, settings.timescale)

    # Predictive-failure + health + KPI engine (server-side replacement for the
    # frontend heuristic). Read-only over forecast/device/asset/alarm services.
    prediction_service = PredictionService(
        forecast_service=forecast_service,
        device_service=device_service,
        asset_service=asset_service,
        alarm_service=alarm_service,
    )

    # Pipeline orchestrator: anomaly alarms -> reasoning -> AnomalyEnriched.
    # The WebSocket broadcaster (intern B1) and reasoning webhook (intern B3)
    # subscribe to the AnomalyEnriched events this publishes.
    pipeline_service = PipelineService(
        event_publisher=event_publisher,
        device_service=device_service,
        copilot_service=copilot_service,
        prediction_service=prediction_service,
        reasoning_enabled=settings.llm.enabled,
    )

    # Live feed buffer for the frontend grid (enriched anomalies + work orders).
    anomaly_feed = AnomalyFeed(event_publisher)

    # WebSocket fan-out: broadcast the same enriched events to live clients.
    ws_manager = ConnectionManager()
    WsBroadcaster(event_publisher, ws_manager)

    # Reasoning webhook (intern B6): fire a webhook on post-reasoning events
    # (AnomalyEnriched + WorkOrderAssigned). Default OFF unless configured.
    WebhookSubscriber(event_publisher, settings.webhook.url, enabled=settings.webhook.enabled)

    logger.info("Container initialized: %d devices loaded", len(devices))

    return Container(
        settings=settings,
        device_service=device_service,
        scenario_service=scenario_service,
        endpoint_service=endpoint_service,
        event_service=event_service,
        telemetry_service=telemetry_service,
        simulation_engine=simulation_engine,
        alarm_service=alarm_service,
        historian_service=historian_service,
        tsdb=tsdb,
        forecast_service=forecast_service,
        forecast_scheduler=forecast_scheduler,
        anomaly_detector=anomaly_detector,
        copilot_service=copilot_service,
        asset_service=asset_service,
        prediction_service=prediction_service,
        pipeline_service=pipeline_service,
        anomaly_feed=anomaly_feed,
        ws_manager=ws_manager,
        alarm_repo=alarm_repo,
        engine=engine,
        event_publisher=event_publisher,
        real_poller=real_poller,
        knx_engine=knx_engine,
        discovery_service=discovery_service,
    )
