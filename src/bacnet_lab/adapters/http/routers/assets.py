from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.adapters.http.schemas import (
    AssetCreate,
    AssetResponse,
    AssetUpdate,
)
from bacnet_lab.domain.models.asset import Asset

router = APIRouter(prefix="/api/assets", tags=["assets"])


def _to_response(asset: Asset) -> AssetResponse:
    return AssetResponse(
        id=asset.id,
        name=asset.name,
        asset_class=asset.asset_class,
        device_id=asset.device_id,
        make=asset.make,
        model=asset.model,
        serial=asset.serial,
        install_date=asset.install_date,
        criticality=asset.criticality,
        location=asset.location,
        parent_id=asset.parent_id,
        created_at=asset.created_at,
    )


@router.get("", response_model=list[AssetResponse])
async def list_assets() -> list[AssetResponse]:
    container = get_container()
    assets = await container.asset_service.list_assets()
    return [_to_response(a) for a in assets]


@router.get("/{asset_id}", response_model=AssetResponse)
async def get_asset(asset_id: str) -> AssetResponse:
    container = get_container()
    asset = await container.asset_service.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _to_response(asset)


@router.post("", response_model=AssetResponse, status_code=201)
async def create_asset(req: AssetCreate) -> AssetResponse:
    container = get_container()
    asset = await container.asset_service.create_asset(
        name=req.name,
        asset_class=req.asset_class,
        device_id=req.device_id,
        make=req.make,
        model=req.model,
        serial=req.serial,
        install_date=req.install_date,
        criticality=req.criticality,
        location=req.location,
        parent_id=req.parent_id,
    )
    return _to_response(asset)


@router.put("/{asset_id}", response_model=AssetResponse)
async def update_asset(asset_id: str, req: AssetUpdate) -> AssetResponse:
    container = get_container()
    asset = await container.asset_service.update_asset(
        asset_id, **req.model_dump(exclude_unset=True)
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _to_response(asset)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(asset_id: str) -> None:
    container = get_container()
    asset = await container.asset_service.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    await container.asset_service.delete_asset(asset_id)
