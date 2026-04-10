"""
api.py — FastAPI chatbot service, được gọi bởi C# backend (CO4029_BE).

Endpoints:
  POST /chat               — nhận {message, history} → trả {type, content}
  GET  /health             — health check cho C# backend
  GET  /buildings          — liệt kê tất cả tòa nhà (public)
  GET  /buildings/{key}    — lấy một tòa nhà theo key (public)
  POST /buildings          — tạo mới tòa nhà (yêu cầu X-API-Key)
  PUT  /buildings/{key}    — cập nhật tòa nhà (yêu cầu X-API-Key)
  DELETE /buildings/{key}  — xóa tòa nhà (yêu cầu X-API-Key)
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from auth import verify_api_key
from bot import run_chatbot
from routers.buildings import router as buildings_router

load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="HCMUT Chatbot Service",
    description="Internal AI service — được gọi bởi CO4029_BE",
    version="1.0.0",
    docs_url="/docs",
)

# ── CORS: cho phép C# backend gọi vào ───────────────────────────────────────
_raw_origins = os.getenv("CHATBOT_ALLOWED_ORIGINS", "*")
allowed_origins = [o.strip() for o in _raw_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["POST", "GET", "PUT", "DELETE"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(buildings_router)


# ============================================================================
# REQUEST / RESPONSE — khớp với ChatbotService.cs trong CO4029_BE
# ============================================================================

class Message(BaseModel):
    """Một lượt hội thoại, gửi từ C# backend."""
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    """
    Body mà C# ChatbotService gửi lên:
        {
          "message": "Chỉ tôi đường đến thư viện",
          "history": [
            {"role": "user",      "content": "..."},
            {"role": "assistant", "content": "..."}
          ]
        }
    """
    message: str = Field(..., min_length=1)
    history: list[Message] = Field(default_factory=list)


class TextResponse(BaseModel):
    """
    Trả về khi LLM trả lời bằng văn bản.
    C# đọc: result.GetProperty("content").GetString()
    """
    type: str = "text"
    content: str


class NavigationResponse(BaseModel):
    """
    Trả về khi LLM gọi trigger_navigation tool.
    C# đọc: result.GetProperty("content").GetProperty("destination_building").GetString()
    """
    type: str = "navigation"
    content: dict  # {"event": "navigation_triggered", "destination_building": "..."}


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/health")
async def health():
    """C# backend gọi endpoint này để kiểm tra service còn sống không."""
    return {"status": "ok"}


@app.post(
    "/chat",
    response_model=TextResponse | NavigationResponse,
    dependencies=[Security(verify_api_key)],
)
async def chat(request: ChatRequest):
    """
    Endpoint chính — C# ChatbotService gọi vào đây.

    Flow:
      C# backend  →  POST /chat  →  Pipecat pipeline  →  Ollama LLM
                  ←  {type, content}

    Response type:
      "text"       → C# lưu content vào Chatbox, trả về frontend
      "navigation" → C# lưu [NAVIGATION] vào Chatbox, gửi event về frontend
    """
    history = [{"role": m.role, "content": m.content} for m in request.history]

    try:
        result = await run_chatbot(request.message, history)
    except Exception:
        logger.exception("Chatbot pipeline error")
        raise HTTPException(status_code=500, detail="Chatbot pipeline failed")

    return result


# ============================================================================
# CHẠY TRỰC TIẾP
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
