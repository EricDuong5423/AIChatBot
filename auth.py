"""
auth.py — API Key authentication dùng chung cho api.py và routers.
"""

import os

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

API_KEY = os.getenv("CHATBOT_API_KEY", "")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(key: str | None = Security(api_key_header)) -> None:
    """Bỏ qua kiểm tra nếu API_KEY chưa được cấu hình (môi trường dev)."""
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
