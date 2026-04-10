"""
seed_buildings.py — Đổ dữ liệu mẫu vào MongoDB.

Chạy một lần sau khi setup MongoDB:
  python seed_buildings.py
"""

import asyncio
from database import _get_collection

SEED_DATA = [
    {
        "key": "A4",
        "ten": "Tòa nhà A4",
        "mo_ta": "Tòa nhà A4 là khu vực giảng dạy chính của Khoa Điện-Điện tử. "
                 "Gồm 8 tầng với các phòng học lớn, phòng thí nghiệm vi xử lý và "
                 "phòng thí nghiệm điện tử công suất.",
        "khoa": "Khoa Điện-Điện tử",
        "tang": 8,
        "dich_vu": ["Phòng học lý thuyết", "Phòng thí nghiệm", "Văn phòng khoa"],
    },
    {
        "key": "B4",
        "ten": "Tòa nhà B4",
        "mo_ta": "Tòa nhà B4 thuộc Khoa Khoa học Máy tính. Có phòng lab máy tính "
                 "hiện đại, phòng nghiên cứu AI và trung tâm dữ liệu nhỏ.",
        "khoa": "Khoa Khoa học Máy tính",
        "tang": 6,
        "dich_vu": ["Lab lập trình", "Phòng nghiên cứu", "Câu lạc bộ sinh viên"],
    },
    {
        "key": "thu-vien",
        "ten": "Thư viện Trung tâm",
        "mo_ta": "Thư viện Trung tâm ĐHBK TP.HCM có hơn 200,000 đầu sách, "
                 "khu vực đọc sách yên tĩnh, phòng họp nhóm và truy cập "
                 "cơ sở dữ liệu học thuật quốc tế 24/7.",
        "khoa": "Phục vụ toàn trường",
        "tang": 4,
        "dich_vu": ["Mượn/trả sách", "Phòng đọc", "Phòng họp nhóm", "In ấn/photocopy"],
    },
    {
        "key": "hoi-truong",
        "ten": "Hội trường A",
        "mo_ta": "Hội trường chính của trường với sức chứa 1,500 người, "
                 "được sử dụng cho lễ tốt nghiệp, hội thảo lớn và sự kiện toàn trường.",
        "khoa": "Ban Quản lý cơ sở vật chất",
        "tang": 1,
        "dich_vu": ["Hội thảo", "Lễ tốt nghiệp", "Sự kiện"],
    },
    {
        "key": "ky-tuc-xa",
        "ten": "Khu Ký túc xá",
        "mo_ta": "Khu ký túc xá gồm 5 dãy nhà với tổng sức chứa hơn 3,000 sinh viên. "
                 "Có căng-tin, khu thể thao và phòng sinh hoạt chung.",
        "khoa": "Ban Quản lý Ký túc xá",
        "tang": 5,
        "dich_vu": ["Phòng ở", "Căng-tin", "Thể thao", "Giặt ủi"],
    },
]


async def main():
    col = _get_collection()
    inserted = 0
    skipped = 0
    for b in SEED_DATA:
        existing = await col.find_one({"key": b["key"]})
        if existing:
            print(f"  [skip] {b['key']} đã tồn tại")
            skipped += 1
        else:
            await col.insert_one(b)
            print(f"  [ok]   {b['key']} — {b['ten']}")
            inserted += 1
    print(f"\nHoàn tất: {inserted} inserted, {skipped} skipped.")


if __name__ == "__main__":
    asyncio.run(main())
