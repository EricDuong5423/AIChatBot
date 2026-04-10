"""
database.py — Kết nối MongoDB và CRUD operations cho buildings.

Dùng motor (async MongoDB driver) để tương thích với FastAPI async.
"""

import os
from typing import Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

load_dotenv()

_client: Optional[AsyncIOMotorClient] = None


def _get_collection() -> AsyncIOMotorCollection:
    """Trả về collection 'buildings'. Tạo client lazy (kết nối lần đầu dùng)."""
    global _client
    if _client is None:
        uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        _client = AsyncIOMotorClient(uri)
    db_name = os.getenv("MONGODB_DB_NAME", "hcmut_chatbot")
    return _client[db_name]["buildings"]


# ============================================================================
# CRUD
# ============================================================================

async def db_get_all_buildings() -> list[dict]:
    """Lấy tất cả tòa nhà, bỏ trường _id của MongoDB."""
    col = _get_collection()
    cursor = col.find({}, {"_id": 0})
    return await cursor.to_list(length=None)


async def db_get_building(key: str) -> Optional[dict]:
    """Lấy một tòa nhà theo key (e.g. 'A4', 'thu-vien')."""
    col = _get_collection()
    return await col.find_one({"key": key}, {"_id": 0})


async def db_create_building(data: dict) -> bool:
    """
    Tạo mới một tòa nhà.
    Trả về True nếu thành công, False nếu key đã tồn tại.
    """
    col = _get_collection()
    existing = await col.find_one({"key": data["key"]})
    if existing:
        return False
    await col.insert_one(data)
    return True


async def db_update_building(key: str, data: dict) -> bool:
    """
    Cập nhật tòa nhà theo key.
    Trả về True nếu tìm thấy và cập nhật, False nếu không tìm thấy.
    """
    col = _get_collection()
    # Không cho phép thay đổi key qua update
    data.pop("key", None)
    result = await col.update_one({"key": key}, {"$set": data})
    return result.matched_count > 0


async def db_delete_building(key: str) -> bool:
    """
    Xóa tòa nhà theo key.
    Trả về True nếu xóa thành công, False nếu không tìm thấy.
    """
    col = _get_collection()
    result = await col.delete_one({"key": key})
    return result.deleted_count > 0
