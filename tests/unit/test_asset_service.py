import pytest
from unittest.mock import AsyncMock, MagicMock

from bacnet_lab.application.asset_service import AssetService, infer_asset_class
from bacnet_lab.domain.models.asset import Asset


class FakeAssetRepo:
    def __init__(self):
        self._store: dict[str, Asset] = {}

    async def save(self, asset: Asset) -> None:
        self._store[asset.id] = asset

    async def get(self, asset_id: str) -> Asset | None:
        return self._store.get(asset_id)

    async def get_all(self) -> list[Asset]:
        return list(self._store.values())

    async def delete(self, asset_id: str) -> None:
        self._store.pop(asset_id, None)


@pytest.mark.asyncio
async def test_create_and_get_asset():
    service = AssetService(repo=FakeAssetRepo())
    asset = await service.create_asset(name="Pump 1", asset_class="Pump", device_id=1001)

    assert asset.id
    assert asset.name == "Pump 1"
    assert asset.asset_class == "Pump"
    assert asset.device_id == 1001
    assert asset.created_at

    fetched = await service.get_asset(asset.id)
    assert fetched is not None
    assert fetched.id == asset.id


@pytest.mark.asyncio
async def test_list_assets():
    service = AssetService(repo=FakeAssetRepo())
    await service.create_asset(name="A", asset_class="Fan")
    await service.create_asset(name="B", asset_class="AHU")

    assets = await service.list_assets()
    assert len(assets) == 2


@pytest.mark.asyncio
async def test_delete_asset():
    service = AssetService(repo=FakeAssetRepo())
    asset = await service.create_asset(name="Chiller", asset_class="Chiller")

    await service.delete_asset(asset.id)
    assert await service.get_asset(asset.id) is None


@pytest.mark.asyncio
async def test_update_asset():
    service = AssetService(repo=FakeAssetRepo())
    asset = await service.create_asset(name="Old", asset_class="Fan", criticality=3)

    updated = await service.update_asset(asset.id, name="New", criticality=5)
    assert updated is not None
    assert updated.name == "New"
    assert updated.criticality == 5


@pytest.mark.asyncio
async def test_update_missing_asset_returns_none():
    service = AssetService(repo=FakeAssetRepo())
    assert await service.update_asset("does-not-exist", name="x") is None


def test_class_inference():
    assert infer_asset_class("AHU-01") == "AHU"
    assert infer_asset_class("Chilled Water Pump") == "Pump"
    assert infer_asset_class("Chiller 2") == "Chiller"
    assert infer_asset_class("Exhaust Fan") == "Fan"
    assert infer_asset_class("Mystery Box") == "Equipment"


@pytest.mark.asyncio
async def test_seed_from_devices_when_empty():
    service = AssetService(repo=FakeAssetRepo())

    dev_a = MagicMock(device_id=1001)
    dev_a.name = "AHU-1"   # `name=` in the MagicMock ctor is reserved, set it as an attr
    dev_b = MagicMock(device_id=1002)
    dev_b.name = "Supply Pump"
    device_service = MagicMock()
    device_service.list_devices = AsyncMock(return_value=[dev_a, dev_b])

    await service.seed_from_devices(device_service)

    assets = await service.list_assets()
    assert len(assets) == 2
    classes = {a.name: a.asset_class for a in assets}
    assert classes["AHU-1"] == "AHU"
    assert classes["Supply Pump"] == "Pump"
    assert all(a.criticality == 3 for a in assets)


@pytest.mark.asyncio
async def test_seed_skips_when_not_empty():
    service = AssetService(repo=FakeAssetRepo())
    await service.create_asset(name="Existing", asset_class="Fan")

    device_service = MagicMock()
    device_service.list_devices = AsyncMock(return_value=[MagicMock(device_id=1, name="AHU")])

    await service.seed_from_devices(device_service)

    assets = await service.list_assets()
    assert len(assets) == 1
    device_service.list_devices.assert_not_called()
