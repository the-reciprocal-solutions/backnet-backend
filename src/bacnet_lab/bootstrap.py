from __future__ import annotations

import logging
from dataclasses import dataclass

from bacnet_lab.adapters.bacnet.device_factory import load_all_devices, scale_devices
from bacnet_lab.adapters.bacnet.engine import BAC0Engine
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
from bacnet_lab.adapters.scenarios.registry import ScenarioRegistry
from bacnet_lab.adapters.webhook.delivery import WebhookDeliveryAdapter
from bacnet_lab.application.alarm_service import AlarmService
from bacnet_lab.application.asset_service import AssetService
from bacnet_lab.application.device_service import DeviceService
from bacnet_lab.adapters.persistence.timescale import TimescaleTimeSeries
from bacnet_lab.application.endpoint_service import EndpointService
from bacnet_lab.application.event_service import EventService
from bacnet_lab.application.historian_service import HistorianService
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
    alarm_repo: SqliteAlarmRepository
    engine: BAC0Engine
    event_publisher: InProcessEventPublisher


async def create_container(settings: AppSettings) -> Container:
    # Run DB migrations
    await run_migrations(settings.db_path)

    # Adapters
    engine = BAC0Engine(ip=settings.bacnet.ip)
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
        network=engine,
        event_publisher=event_publisher,
        bacnet_port_start=settings.bacnet.port_start,
    )

    # Scenarios
    scenario_registry = ScenarioRegistry()
    scenario_registry.register(HvacDayCycleScenario(device_service, event_publisher))
    scenario_registry.register(AlarmScenario(device_service, event_publisher))
    scenario_registry.register(DeviceOfflineScenario(device_service, event_publisher))
    scenario_registry.register(ManualOverrideScenario(device_service, event_publisher))

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

    # Load and initialize devices (optionally scaled to a target count)
    devices = load_all_devices(settings.devices_dir)
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

    # Forecasting (Chronos + own DB access; naive fallback when torch absent)
    forecast_service = ForecastService(settings.timescale.dsn)

    # Periodic forecast refresh — keeps forecasts fresh for detector + UI
    forecast_scheduler = ForecastScheduler(
        forecast_service=forecast_service,
        device_service=device_service,
        settings=settings.forecast,
    )

    # Anomaly Detection (compares live points against forecasts)
    anomaly_detector = AnomalyDetector(
        event_publisher=event_publisher,
        db=forecast_service.db,
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
        alarm_repo=alarm_repo,
        engine=engine,
        event_publisher=event_publisher,
    )
