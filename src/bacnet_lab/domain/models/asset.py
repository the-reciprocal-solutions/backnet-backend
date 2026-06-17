from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Asset:
    id: str
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
    created_at: str = ""
