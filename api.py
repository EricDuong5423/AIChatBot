"""
api.py — FastAPI chatbot service, được gọi bởi C# backend (CO4029_BE).

Endpoints:
  POST /chat    — nhận {message, history} → trả {type, content}
  GET  /health  — health check cho C# backend

Buildings info được lưu dưới dạng markdown trong docs/buildings/ và truy cập
qua RAG (tool search_knowledge). Không còn MongoDB.
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import verify_api_key
from bot import run_chatbot
from formatter import convert as convert_format
from rag import query_vectorstore
from routers.docs import router as docs_router

load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="HCMUT Chatbot Service",
    description="Internal AI service — được gọi bởi CO4029_BE",
    version="1.0.0",
    docs_url="/swagger",  # /docs reserved cho knowledge base router
)

# ── CORS: cho phép C# backend gọi vào ───────────────────────────────────────
_raw_origins = os.getenv("CHATBOT_ALLOWED_ORIGINS", "*")
allowed_origins = [o.strip() for o in _raw_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["POST", "GET", "DELETE"],
    allow_headers=["*"],
)

app.include_router(docs_router)


@app.on_event("startup")
async def _warmup_rag() -> None:
    """
    Khởi tạo RAG khi server start:
    1. Nếu chưa có chroma_db/ nhưng có docs/ → tự build index (cho Render cold deploy)
    2. Warm up embedding model qua 1 query "warmup" để query đầu không tốn ~10s
    """
    import asyncio as _asyncio
    import os
    from rag import build_vectorstore, CHROMA_DIR, DOCS_DIR

    has_chroma = os.path.isdir(CHROMA_DIR) and any(os.scandir(CHROMA_DIR))
    has_docs = os.path.isdir(DOCS_DIR) and any(p.suffix == ".md" for p in __import__("pathlib").Path(DOCS_DIR).rglob("*.md"))

    if not has_chroma and has_docs:
        logger.info("chroma_db trống nhưng docs/ có .md → tự build index khi startup")
        try:
            n = await _asyncio.to_thread(build_vectorstore)
            logger.info("Auto-built index: %d chunks", n)
        except Exception as e:
            logger.warning("Auto-build index lỗi: %s", e)

    try:
        await _asyncio.to_thread(query_vectorstore, "warmup", 1)
        logger.info("RAG warmup done (embeddings cached)")
    except Exception as e:
        logger.warning("RAG warmup failed: %s", e)


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
async def chat(
    request: ChatRequest,
    format: str = Query(
        "md",
        pattern="^(md|tmp|plain)$",
        description=(
            "Format text response: "
            "'md' (default, markdown gốc cho web), "
            "'tmp' (TextMeshPro rich text cho Unity), "
            "'plain' (strip markdown). Không ảnh hưởng response navigation."
        ),
    ),
):
    """
    Endpoint chính — gọi từ C# backend (CO4029_BE).

    Flow:
      Client  →  POST /chat?format=<md|tmp|plain>  →  Pipecat pipeline  →  LLM
              ←  {type, content}

    Response type:
      "text"       → content là string đã convert theo format
      "navigation" → content là dict {event, destination_building}, không convert
    """
    history = [{"role": m.role, "content": m.content} for m in request.history]

    try:
        result = await run_chatbot(request.message, history)
    except Exception:
        logger.exception("Chatbot pipeline error")
        raise HTTPException(status_code=500, detail="Chatbot pipeline failed")

    # Chỉ convert text content; navigation giữ nguyên (dict structured data)
    if result.get("type") == "text" and format != "md":
        result["content"] = convert_format(result["content"], format)

    return result


# ============================================================================
# FRONTEND — serve Vue build (chỉ khi dist/ đã được build)
# ============================================================================
_dist = os.path.join(os.path.dirname(__file__), "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")


# ============================================================================
# CHẠY TRỰC TIẾP
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
