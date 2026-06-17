from __future__ import annotations

import uuid
from datetime import datetime, timezone

from bacnet_lab.domain.models.asset import Asset
from bacnet_lab.ports.repositories import AssetRepositoryPort


def infer_asset_class(name: str) -> str:
    lowered = (name or "").lower()
    if "ahu" in lowered:
        return "AHU"
    if "pump" in lowered:
        return "Pump"
    if "chiller" in lowered:
        return "Chiller"
    if "fan" in lowered:
        return "Fan"
    return "Equipment"


class AssetService:
    def __init__(self, repo: AssetRepositoryPort) -> None:
        self._repo = repo

    async def list_assets(self) -> list[Asset]:
        return await self._repo.get_all()

    async def get_asset(self, asset_id: str) -> Asset | None:
        return await self._repo.get(asset_id)

    async def create_asset(
        self,
        name: str,
        asset_class: str,
        device_id: int | None = None,
        make: str = "",
        model: str = "",
        serial: str = "",
        install_date: str | None = None,
        criticality: int = 3,
        location: str = "",
        parent_id: str | None = None,
    ) -> Asset:
        asset = Asset(
            id=str(uuid.uuid4()),
            name=name,
            asset_class=asset_class,
            device_id=device_id,
            make=make,
            model=model,
            serial=serial,
            install_date=install_date,
            criticality=criticality,
            location=location,
            parent_id=parent_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._repo.save(asset)
        return asset

    async def update_asset(self, asset_id: str, **fields) -> Asset | None:
        asset = await self._repo.get(asset_id)
        if not asset:
            return None
        for key, value in fields.items():
            if value is not None and hasattr(asset, key):
                setattr(asset, key, value)
        await self._repo.save(asset)
        return asset

    async def delete_asset(self, asset_id: str) -> None:
        await self._repo.delete(asset_id)

    async def seed_from_devices(self, device_service) -> None:
        existing = await self._repo.get_all()
        if existing:
            return
        devices = await device_service.list_devices()
        for device in devices:
            await self.create_asset(
                name=device.name,
                asset_class=infer_asset_class(device.name),
                device_id=device.device_id,
                criticality=3,
            )
