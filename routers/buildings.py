"""
routers/buildings.py — CRUD API cho thông tin tòa nhà HCMUT.

Endpoints:
  GET    /buildings          — liệt kê tất cả (public)
  GET    /buildings/{key}    — lấy một tòa nhà theo key (public)
  POST   /buildings          — tạo mới (yêu cầu X-API-Key)
  PUT    /buildings/{key}    — cập nhật (yêu cầu X-API-Key)
  DELETE /buildings/{key}    — xóa (yêu cầu X-API-Key)
"""

from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field

from auth import verify_api_key
from database import (
    db_create_building,
    db_delete_building,
    db_get_all_buildings,
    db_get_building,
    db_update_building,
)

router = APIRouter(prefix="/buildings", tags=["buildings"])


# ============================================================================
# SCHEMA
# ============================================================================

class BuildingCreate(BaseModel):
    """Body để tạo mới tòa nhà."""
    key: str = Field(..., description="Slug định danh duy nhất, ví dụ: 'A4', 'thu-vien'")
    ten: str = Field(..., description="Tên đầy đủ, ví dụ: 'Tòa nhà A4'")
    mo_ta: str = Field(..., description="Mô tả chi tiết về tòa nhà")
    khoa: str = Field(..., description="Khoa/đơn vị đóng tại tòa nhà")
    tang: int = Field(..., ge=1, description="Số tầng")
    dich_vu: list[str] = Field(default_factory=list, description="Danh sách dịch vụ/tiện ích")


class BuildingUpdate(BaseModel):
    """Body để cập nhật — tất cả trường đều optional."""
    ten: str | None = None
    mo_ta: str | None = None
    khoa: str | None = None
    tang: int | None = Field(default=None, ge=1)
    dich_vu: list[str] | None = None


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("")
async def list_buildings():
    """Liệt kê tất cả tòa nhà trong DB."""
    return await db_get_all_buildings()


@router.get("/{key}")
async def get_building(key: str):
    """Lấy thông tin một tòa nhà theo key."""
    building = await db_get_building(key)
    if not building:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tòa nhà với key='{key}'")
    return building


@router.post("", status_code=201, dependencies=[Security(verify_api_key)])
async def create_building(body: BuildingCreate):
    """Tạo mới tòa nhà. Key phải là duy nhất."""
    created = await db_create_building(body.model_dump())
    if not created:
        raise HTTPException(status_code=409, detail=f"Key '{body.key}' đã tồn tại")
    return {"message": f"Đã tạo tòa nhà '{body.key}'"}


@router.put("/{key}", dependencies=[Security(verify_api_key)])
async def update_building(key: str, body: BuildingUpdate):
    """Cập nhật thông tin tòa nhà. Chỉ gửi các trường cần thay đổi."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Không có trường nào để cập nhật")
    updated = await db_update_building(key, updates)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tòa nhà với key='{key}'")
    return {"message": f"Đã cập nhật tòa nhà '{key}'"}


@router.delete("/{key}", dependencies=[Security(verify_api_key)])
async def delete_building(key: str):
    """Xóa tòa nhà theo key."""
    deleted = await db_delete_building(key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tòa nhà với key='{key}'")
    return {"message": f"Đã xóa tòa nhà '{key}'"}
