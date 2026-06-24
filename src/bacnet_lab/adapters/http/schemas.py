from __future__ import annotations

from pydantic import BaseModel

from bacnet_lab.domain.enums import EventType


class PointResponse(BaseModel):
    object_type: str
    object_instance: int
    object_name: str
    description: str
    present_value: float | int | bool | str
    units: str


class DeviceResponse(BaseModel):
    device_id: int
    name: str
    description: str
    status: str
    point_count: int
    protocol: str


class DeviceDetailResponse(BaseModel):
    device_id: int
    name: str
    description: str
    status: str
    protocol: str
    points: list[PointResponse]


class WritePointRequest(BaseModel):
    object_type: str
    object_instance: int
    value: float | int | bool | str


class WritePointByNameRequest(BaseModel):
    point_name: str
    value: float | int | bool | str


class ScenarioResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str


class StartScenarioRequest(BaseModel):
    params: dict | None = None


class EndpointCreateRequest(BaseModel):
    url: str
    event_types: list[EventType] | None = None


class EndpointResponse(BaseModel):
    id: str
    url: str
    secret: str
    enabled: bool
    event_types: list[str]
    failure_count: int


class EventResponse(BaseModel):
    id: str
    event_type: str
    timestamp: str
    payload: dict
    delivered: bool


class AlarmResponse(BaseModel):
    id: str
    device_id: int
    point_name: str
    severity: str
    message: str
    raised_at: str
    cleared_at: str | None


class AssetCreate(BaseModel):
    name: str
    asset_class: str
    device_id: int | None = None
    make: str = ""
    model: str = ""
    serial: str = ""
    install_date: str | None = None
    criticality: int = 3
    location: str = ""
    parent_id: str | None = None


class AssetUpdate(BaseModel):
    name: str | None = None
    asset_class: str | None = None
    device_id: int | None = None
    make: str | None = None
    model: str | None = None
    serial: str | None = None
    install_date: str | None = None
    criticality: int | None = None
    location: str | None = None
    parent_id: str | None = None


class AssetResponse(BaseModel):
    id: str
    name: str
    asset_class: str
    device_id: int | None
    make: str
    model: str
    serial: str
    install_date: str | None
    criticality: int
    location: str
    parent_id: str | None
    created_at: str


class HealthResponse(BaseModel):
    status: str
    version: str
    devices_count: int
    active_scenarios: int


class SnapshotPointResponse(BaseModel):
    device_id: int
    device_name: str
    point_name: str
    object_type: str
    value: float | int | bool | str
    units: str


class PredictionItem(BaseModel):
    point: str
    device_id: int | None
    units: str
    current: float | None
    predicted: float | None
    bound: float | None
    direction: str
    eta_minutes: int | None
    confidence: str
    level: str
    reason: str


class AssetHealth(BaseModel):
    asset_id: str
    name: str
    score: int
    status: str
    active_alarms: int
    predictions: list[PredictionItem]
    rul_minutes: int | None


class FleetKpi(BaseModel):
    avg_health: int
    assets_total: int
    assets_at_risk: int
    active_alarms: int
    predicted_failures: int
    by_level: dict[str, int]
