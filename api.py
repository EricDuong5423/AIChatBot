import asyncio
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional

from auth import verify_api_key
from bot import run_chatbot
from chat_history import fetch_history, save_exchange
from formatter import convert as convert_format
from rag import query_vectorstore
from routers.docs import router as docs_router

load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="HCMUT Chatbot Service",
    description="Internal AI service — được gọi bởi CO4029_BE",
    version="1.0.0",
    docs_url="/swagger",
)

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
    import asyncio as _asyncio
    from pathlib import Path
    from rag import build_vectorstore, DATABASE_URL, DOCS_DIR

    if not DATABASE_URL:
        logger.warning("DATABASE_URL chưa set — RAG disabled (set trong .env hoặc Render dashboard)")
        return

    has_docs = os.path.isdir(DOCS_DIR) and any(
        Path(DOCS_DIR).rglob("*.md")
    )

    try:
        await _asyncio.to_thread(query_vectorstore, "warmup", 1)
        logger.info("RAG warmup done (Supabase pgvector OK)")
    except Exception:
        if has_docs:
            logger.info("Supabase collection trống → tự build index từ docs/")
            try:
                n = await _asyncio.to_thread(build_vectorstore)
                logger.info("Auto-built index: %d chunks", n)
            except Exception as e:
                logger.warning("Auto-build index lỗi: %s", e)
        else:
            logger.warning("RAG warmup failed và không có docs/ để build — upload docs trước")

class Message(BaseModel):
    """Một lượt hội thoại, gửi từ C# backend."""
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    """
    Body mà C# ChatbotService gửi lên.

    Có 2 chế độ:
    1. Truyền session_id + user_id (khuyến nghị — C# backend dùng cách này):
       {
         "message":    "Chỉ tôi đường đến thư viện",
         "session_id": "uuid-của-phiên-chat",   // = id bảng history (cũng nhận "history_id")
         "user_id":    "uuid-của-user"          // optional, cho logging/trace
       }
       → Python tự fetch chatbox từ Supabase theo session, chạy LLM, rồi tự lưu
         cả user message + assistant response trở lại chatbox.

    2. Truyền history array (backward compat, không lưu lại Supabase):
       {
         "message": "...",
         "history": [{"role": "user", "content": "..."}, ...]
       }
    """
    message: str = Field(..., min_length=1)
    # Phiên chat — nhận cả "session_id" lẫn "history_id" (cùng là UUID bảng history)
    session_id: Optional[str] = None
    history_id: Optional[str] = None
    user_id: Optional[str] = None           # UUID của user (logging/trace only)
    history: list[Message] = Field(default_factory=list)  # fallback khi không có session

    @property
    def session(self) -> Optional[str]:
        """UUID phiên chat — ưu tiên session_id, fallback history_id."""
        return self.session_id or self.history_id


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
    # Nếu có session (session_id/history_id) → fetch lịch sử từ Supabase, bỏ qua history array
    sid = request.session
    if sid:
        history = await asyncio.to_thread(fetch_history, sid)
        logger.debug("Fetched %d messages for session=%s user=%s",
                     len(history), sid, request.user_id)
    else:
        history = [{"role": m.role, "content": m.content} for m in request.history]

    try:
        result = await run_chatbot(request.message, history)
    except Exception:
        logger.exception("Chatbot pipeline error")
        raise HTTPException(status_code=500, detail="Chatbot pipeline failed")

    # Lưu cặp user+assistant vào chatbox (đồng bộ — đảm bảo lần chat kế có đủ context).
    # Lưu RAW markdown (trước khi convert format) để DB luôn giữ bản gốc.
    if sid:
        if result.get("type") == "navigation":
            dest = result["content"].get("destination_building", "?")
            to_save = f"[Dẫn đường: {dest}]"
        else:
            to_save = result.get("content", "")
        await asyncio.to_thread(save_exchange, sid, request.message, to_save)

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
