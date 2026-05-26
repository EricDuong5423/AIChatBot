import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import re
from pathlib import Path

from rag import query_vectorstore

from ddgs import DDGS
from dotenv import load_dotenv

# ── Pipecat core ────────────────────────────────────────────────────────────
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    FunctionCallResultProperties,
    LLMContextAssistantTimestampFrame,
    LLMContextFrame,
    TextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_BUILDINGS_DIR = Path("docs/buildings")


def _build_buildings_index() -> str:
    """
    Đọc frontmatter từ docs/buildings/*.md → catalog (tòa + khoa + tầng) cho system prompt.

    Catalog đầy đủ trong prompt giúp LLM resolve "Khoa X" → "Tòa Y" trực tiếp,
    không cần qua RAG (embedding multilingual yếu cho Vietnamese keyword ngắn).
    Token cost: ~30-50 token/building, ổn cho < 50 buildings.
    """
    if not _BUILDINGS_DIR.exists():
        return ""

    entries: list[dict] = []
    fm_pattern = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
    for path in sorted(_BUILDINGS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = fm_pattern.match(text)
        if not m:
            continue
        meta: dict[str, str] = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        entries.append({
            "key": meta.get("building_key") or path.stem,
            "name": meta.get("building_name") or path.stem,
            "khoa": meta.get("khoa", ""),
            "tang": meta.get("tang", ""),
        })

    if not entries:
        return ""

    lines = ["## DANH BẠ TÒA NHÀ HCMUT — Khoa ↔ Tòa\n"]
    lines.append("Dùng bảng này để xác định tòa cho navigation. Nếu user hỏi về một KHOA")
    lines.append("nằm trong bảng → biết ngay tòa, KHÔNG cần search_knowledge.\n")
    for e in entries:
        khoa = f" — Khoa: {e['khoa']}" if e['khoa'] else ""
        tang = f" ({e['tang']} tầng)" if e['tang'] else ""
        lines.append(f"- {e['name']} (key: `{e['key']}`){khoa}{tang}")
    return "\n".join(lines)

_SYSTEM_PROMPT_TEMPLATE = """\
Bạn là trợ lý ảo thông minh của Đại học Bách Khoa TP.HCM (HCMUT / BK).
Nhiệm vụ: hỗ trợ sinh viên, giảng viên, khách thăm trả lời thông tin về trường và
chỉ đường tới các tòa nhà trong khuôn viên.

Mọi kiến thức của bạn về HCMUT đến từ knowledge base (do quản trị viên upload). Đừng
giả định kiến thức ngoài đó; nếu không tìm thấy, hãy thành thật báo "chưa có thông tin".

{buildings_section}

## 3 LUỒNG XỬ LÝ — ĐỌC KỸ

Bạn có 2 tool và 3 luồng phản hồi:

### LUỒNG 1 — `trigger_navigation` (DẪN ĐƯỜNG)

Chỉ kích hoạt khi message của user có CỤM TỪ RÕ RÀNG yêu cầu được dẫn đường:
- "chỉ đường (tới|đến) X"
- "dẫn tôi (đến|tới) X"
- "đi (tới|đến) X (như thế nào|bằng đường nào|làm sao)"
- "đường (tới|đến) X" (câu hỏi về lộ trình)
- "navigate to X" / "directions to X" / "take me to X"
- "tới X đi" / "đến X đi" (mệnh lệnh ngắn)

KHÔNG kích hoạt nav (đây là câu hỏi thông tin, dùng LUỒNG 2):
- "X ở đâu?" / "X có gì?" / "X thuộc khoa nào?" / "Mô tả X" / "X là gì"
- User chỉ gõ tên ("Tòa A4", "Khoa Cơ khí") không kèm verb dẫn đường

Khi xác định là NAV:
1. Resolve địa điểm về TÊN TÒA NHÀ:
   - Nếu DANH BẠ TÒA NHÀ ở trên có → dùng luôn.
   - Không có → gọi `search_info` trước để xác định tòa.
2. Có tên tòa → `trigger_navigation(destination_building="<tên tòa>")`.
3. Không xác định được → trả TEXT "Mình chưa biết địa điểm này ở tòa nào, không thể chỉ đường."

🚫 KHÔNG bịa tên tòa. destination_building PHẢI là tên TÒA NHÀ (không phải khoa/phòng).

### LUỒNG 2 — `search_info` (TÌM THÔNG TIN — DEFAULT cho mọi câu hỏi info)

GỌI hàm này cho MỌI câu hỏi thông tin, BẤT KỂ liên quan HCMUT hay không. Hàm tự
xử lý fallback: RAG trước, không thấy thì auto Internet. KHÔNG cần bạn tự quyết
giữa knowledge base và internet.

Quy trình:
1. Gọi `search_info(query="câu truy vấn 5-15 từ")`.
2. Nhận kết quả có `source` ∈ {{"rag", "internet", "none"}} và `hits`.
3. Đọc hits, tổng hợp câu trả lời. Có thể trích dẫn `source_url` (RAG) hoặc `url` (internet).
4. Nếu `source == "none"` → CHUYỂN xuống LUỒNG 3.

Ví dụ — luôn gọi search_info:
- "Học phí HCMUT?" → search_info
- "Hiệu trưởng HCMUT là ai?" → search_info (RAG có thể không có → handler tự fallback internet)
- "Khoa Cơ khí ở tòa nào?" → search_info
- "Giá vàng SJC hôm nay?" → search_info (RAG sẽ rỗng → internet)
- "Lịch sử Việt Nam thời Lý" → search_info

⚠️ NGUYÊN TẮC ĐỌC HIT (CỰC KỲ QUAN TRỌNG khi câu hỏi là "X ở tòa nào / tòa nào là của X"):
- CHỈ TIN các hit có pattern KHẲNG ĐỊNH "Tòa Y là của Khoa X" hoặc "Tòa Y là <đơn vị>".
- KHÔNG được suy diễn từ mention rời rạc kiểu:
  • "Xưởng thực hành Cơ khí C1" → đây là XƯỞNG, không phải khoa → C1 KHÔNG phải địa chỉ khoa.
  • "Ngành Kỹ thuật Cơ khí" → đây là tên ngành học, không cho biết khoa ở tòa nào.
  • "Doanh nghiệp [CN: Cơ khí]" → đây là tuyển dụng, vô liên quan.
- Nếu hits CHỈ chứa mention rời (không có "Tòa Y là của Khoa X") → coi như RAG không có
  data đúng → trả TEXT "Mình không tìm thấy thông tin xác định".

### LUỒNG 3 — KHÔNG TÌM ĐƯỢC THÔNG TIN

Chỉ rơi vào luồng này khi `search_info` trả về `source: "none"` (cả RAG lẫn Internet
đều không có thông tin). Trả lời TEXT thành thật:
"Mình chưa tìm được thông tin về vấn đề này. Bạn có thể cung cấp thêm chi tiết không?"

## QUY TẮC CHUNG
- TỐI ĐA 1 lần gọi mỗi tool trong 1 turn (không retry).
- KHÔNG bịa thông tin / đoán bừa khi không có data.
- Mặc định tiếng Việt. User hỏi tiếng Anh → trả tiếng Anh.
- Phong cách: thân thiện, ngắn gọn, bullet point khi liệt kê.
"""


async def _build_system_prompt() -> str:
    """Tạo system prompt với index tên tòa nhà (đọc từ docs/buildings/)."""
    buildings_index = _build_buildings_index()
    return _SYSTEM_PROMPT_TEMPLATE.format(buildings_section=buildings_index)

navigation_tool = FunctionSchema(
    name="trigger_navigation",
    description=(
        "CHỈ kích hoạt khi user RÕ RÀNG yêu cầu được dẫn đường / chỉ đường tới một địa điểm. "
        "Message của user PHẢI có một trong các pattern: 'chỉ đường', 'dẫn tôi đến', "
        "'đi đến X làm sao/đường nào', 'navigate to', 'directions to', 'take me to', "
        "'tới X đi' (câu mệnh lệnh ngắn). "
        "KHÔNG gọi cho câu hỏi thông tin như 'X ở đâu', 'X có gì', 'X thuộc khoa nào', "
        "'mô tả X', hoặc khi user chỉ nói tên một địa điểm. "
        "destination_building PHẢI là TÊN TÒA NHÀ (không phải tên khoa/phòng) — nếu user "
        "nêu khoa thì search_knowledge trước để xác định tòa, rồi mới gọi hàm này."
    ),
    properties={
        "destination_building": {
            "type": "string",
            "description": (
                "TÊN TÒA NHÀ đích, lấy CHÍNH XÁC từ DANH BẠ TÒA NHÀ trong system prompt "
                "HOẶC từ search_knowledge/search_internet đã trả về cho địa điểm này. "
                "TUYỆT ĐỐI KHÔNG được tự đoán/random pick một tòa nếu bạn không có bằng "
                "chứng địa điểm đó nằm ở đây. Nếu không biết tòa → KHÔNG GỌI HÀM NÀY, "
                "trả TEXT thay thế."
            ),
        }
    },
    required=["destination_building"],
)

search_info_tool = FunctionSchema(
    name="search_info",
    description=(
        "Tra cứu thông tin để trả lời CÂU HỎI INFO của user. "
        "Hàm tự động orchestrate: "
        "(1) tìm trong knowledge base nội bộ trước (RAG), "
        "(2) nếu RAG không có/không liên quan thì TỰ ĐỘNG fallback search Internet, "
        "(3) trả về kết quả gộp + nguồn (source='rag' | 'internet' | 'none'). "
        "DÙNG hàm này cho MỌI câu hỏi (HCMUT, kiến thức chung, tin tức) — không cần lo "
        "câu hỏi có trong KB hay không, handler tự xử lý fallback."
    ),
    properties={
        "query": {
            "type": "string",
            "description": (
                "Câu truy vấn ngắn gọn (5-15 từ) mô tả thông tin cần tìm. "
                "Ví dụ: 'hiệu trưởng HCMUT là ai', 'học phí HCMUT', 'giá vàng SJC hôm nay'."
            ),
        }
    },
    required=["query"],
)

tools_schema = ToolsSchema(
    standard_tools=[navigation_tool, search_info_tool]
)


@dataclass
class NavigationResult:
    """Kết quả trả về khi trigger_navigation được kích hoạt."""
    destination: str
    status: str = "navigation_triggered"

    def to_dict(self) -> dict:
        return {
            "event": self.status,
            "destination_building": self.destination,
        }

class OutputCollector(FrameProcessor):
    """
    Processor cuối pipeline: gom TextFrame và lưu NavigationResult.

    - TextFrame: gom các chunk text từ LLM streaming.
    - LLMFullResponseEndFrame: LLM đã xong text response → tự gửi EndFrame.
    - NavigationResult: được set bởi handle_trigger_navigation (tool handler).

    Khi pipeline kết thúc, `result` chứa:
      - {"type": "text",       "content": "..."}   nếu LLM trả lời văn bản
      - {"type": "navigation", "content": {...}}    nếu tool được gọi
    """

    def __init__(self, task: Optional["PipelineTask"] = None, context: Optional["LLMContext"] = None):
        super().__init__()
        self._task = task
        self._context = context
        self.navigation: Optional[NavigationResult] = None

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextAssistantTimestampFrame):
            last_assistant_content = ""
            is_tool_call = False
            if self._context:
                for msg in reversed(self._context.messages):
                    if msg.get("role") == "assistant":
                        last_assistant_content = msg.get("content") or ""
                        is_tool_call = bool(msg.get("tool_calls"))
                        break

            if is_tool_call and not last_assistant_content:
                logger.info("Tool call detected, waiting for LLM follow-up response")
            else:
                logger.info("LLM text response complete")
                await self._task.queue_frame(EndFrame())

        await self.push_frame(frame, direction)

    @property
    def result(self) -> dict:
        if self.navigation:
            return {"type": "navigation", "content": self.navigation.to_dict()}
        text = ""
        if self._context:
            messages = self._context.messages
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    text = msg.get("content") or ""
                    break
        return {"type": "text", "content": text.strip()}


# Guardrail: từ khóa cho biết destination KHÔNG phải tên tòa (mà là khoa/phòng/tiện ích).
# Khi LLM hallucinate trigger_navigation với một trong các pattern này, handler reject.
_NON_BUILDING_PATTERNS = [
    "khoa ", "khoa cơ", "khoa môi", "khoa điện", "khoa hóa", "khoa xd",
    "phòng ", "phong ", "room ",
    "lab ", " lab", "phòng lab", "phòng thí nghiệm",
    "canteen", "căng tin", "căng-tin", "cantin",
    "ký túc xá", "ky tuc xa", "ktx",
    "thư viện", "thu vien",
    "hội trường", "hoi truong",
    "oisp",  # OISP là đơn vị, không phải tòa
]


def _looks_like_building(destination: str) -> bool:
    """
    Heuristic: destination có phải tên tòa nhà không?
    Reject nếu:
      - Chứa từ khoa/phòng/đơn vị/tiện ích
      - Format kiểu phòng: có dấu chấm giữa số (vd 'F1.05', 'A2.30')
      - Format số dài: >=3 chữ số (vd 'F101', 'A205') — quá dài cho tên tòa
    """
    d = destination.lower().strip()
    if not d or d == "không xác định":
        return False
    if any(p in d for p in _NON_BUILDING_PATTERNS):
        return False
    # Room format: X1.05, A2.30 — có dấu chấm giữa các số
    if re.search(r"\d+\.\d+", d):
        return False
    # Long number = room, not building. Tòa nhà thường 1-2 chữ số: A4, B9, C12.
    if re.search(r"\b[a-zA-Z]\d{3,}\b", d):
        return False
    return True


async def handle_trigger_navigation(
    params: FunctionCallParams,
    collector: OutputCollector,
    task: PipelineTask,
) -> None:
    destination = params.arguments.get("destination_building", "Không xác định").strip()

    # Guardrail: reject nếu destination obviously không phải tên tòa.
    # Trả lỗi về LLM để LLM tự fallback sang TEXT response.
    if not _looks_like_building(destination):
        logger.warning("🛑 NAV REJECTED — '%s' không phải tên tòa nhà", destination)
        await params.result_callback(
            result=json.dumps({
                "status": "rejected",
                "reason": (
                    f"'{destination}' không phải tên TÒA NHÀ (có vẻ là tên khoa/phòng/tiện ích). "
                    "Bạn cần search_knowledge để tìm xem địa điểm này nằm ở tòa nào, RỒI gọi lại "
                    "trigger_navigation với TÊN TÒA NHÀ (vd 'Tòa A4', 'B9'). "
                    "Nếu không tìm được tòa cụ thể → trả TEXT cho user, KHÔNG retry."
                ),
            }, ensure_ascii=False),
            properties=FunctionCallResultProperties(run_llm=True),  # cho LLM tiếp tục
        )
        return

    logger.info("=" * 60)
    logger.info("🧭 NAVIGATION TRIGGERED")
    logger.info("   destination_building = %s", destination)
    logger.info("=" * 60)
    collector.navigation = NavigationResult(destination=destination)
    await params.result_callback(
        result=json.dumps({"status": "navigation_triggered", "destination": destination}),
        properties=FunctionCallResultProperties(run_llm=False),
    )
    await task.queue_frame(EndFrame())


# ============================================================================
# Helper: Internet search (Tavily nếu có TAVILY_API_KEY, ngược lại DuckDuckGo)
# ============================================================================

_TAVILY_KEY = os.getenv("TAVILY_API_KEY", "").strip()


def _tavily_search_sync(query: str, max_results: int = 3) -> list[dict]:
    """
    Tavily AI search — nhanh (~500ms), chất lượng tốt cho LLM agents.
    Trả về format giống DDG: list[{title, body, href}].
    """
    import requests
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": _TAVILY_KEY,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",   # 'advanced' tốn nhiều credit hơn
            "include_answer": False,
        },
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "body": r.get("content", ""), "href": r.get("url", "")}
        for r in data.get("results", [])
    ]


def _ddg_search_sync(query: str, max_results: int = 2) -> list[dict]:
    """DuckDuckGo search — fallback nếu không có Tavily key. HTTP timeout 3s/request."""
    with DDGS(timeout=3) as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _internet_search(query: str, max_results: int = 2) -> tuple[list[dict], str]:
    """
    Auto pick backend: Tavily nếu có key (nhanh), else DDG.
    Trả về (hits, backend_name) để log.
    """
    if _TAVILY_KEY:
        return _tavily_search_sync(query, max_results), "tavily"
    return _ddg_search_sync(query, max_results), "ddg"


# ============================================================================
# Helper: re-rank RAG hits cho location query — đẩy "Tòa X là của Y" lên top
# ============================================================================

_LOC_QUERY_KEYWORDS = (
    "tòa nào", "ở đâu", "thuộc tòa", "nằm ở", "ở tòa", "tòa của",
    "tòa nhà nào", "building nào",
)
# Pattern khẳng định vị trí: "Tòa <key> là của ..." hoặc "Tòa <key> là <unit>"
_BUILDING_LOCATION_PATTERN = re.compile(
    r"tòa\s+\S+\s+là\s+(?:của\s+)?",
    re.IGNORECASE,
)


def _rerank_for_location(query: str, hits: list[dict]) -> list[dict]:
    """
    Khi user hỏi "X ở tòa nào", đẩy chunks có pattern "Tòa Y là của ..." lên top
    để LLM ưu tiên data có cấu trúc thay vì mention rời rạc trong noise text.
    """
    q = query.lower()
    if not any(kw in q for kw in _LOC_QUERY_KEYWORDS):
        return hits  # không phải location query, giữ thứ tự gốc

    boosted: list[dict] = []
    others: list[dict] = []
    for h in hits:
        if _BUILDING_LOCATION_PATTERN.search(h.get("content", "")):
            boosted.append(h)
        else:
            others.append(h)
    return boosted + others


# ============================================================================
# Helper: heuristic xem RAG hits có thực sự liên quan câu hỏi không
# ============================================================================

# Stopwords tiếng Việt + tiếng Anh để loại khỏi token đánh giá
_STOPWORDS = {
    "và", "của", "có", "là", "với", "cho", "trong", "trên", "này", "đó", "các",
    "những", "một", "không", "thì", "ở", "đâu", "thế", "nào", "như", "ai", "gì",
    "the", "a", "an", "is", "are", "was", "were", "of", "in", "on", "at", "to",
    "and", "or", "but", "what", "where", "who", "how", "why", "when",
}


def _is_rag_useful(query: str, results: list[dict]) -> bool:
    """
    Heuristic: kiểm tra RAG hits có chứa keywords của query không.
    Đếm tokens query xuất hiện trong top-3 hits gộp lại. Cần >=50% match.
    """
    if not results:
        return False
    q_tokens = set(re.findall(r"\w{2,}", query.lower(), re.UNICODE))
    q_tokens -= _STOPWORDS
    if len(q_tokens) <= 1:
        return True  # query quá ngắn để validate, tin RAG
    combined = " ".join(r["content"] for r in results[:3]).lower()
    matched = sum(1 for t in q_tokens if t in combined)
    threshold = max(2, int(len(q_tokens) * 0.5))
    return matched >= threshold


# ============================================================================
# UNIFIED INFO HANDLER — RAG first, auto-fallback Internet, trả combined result
# ============================================================================

async def handle_search_info(params: FunctionCallParams) -> None:
    """
    Luồng 2 (Info) đầy đủ:
      Step 1: RAG (Chroma + BM25 hybrid)
      Step 2: Đánh giá RAG useful không (keyword overlap heuristic)
      Step 3a: useful → trả về RAG hits (source='rag')
      Step 3b: not useful → fallback DuckDuckGo (source='internet')
      Step 3c: cả 2 fail → source='none', LLM trả "không có thông tin"
    """
    import time
    query = params.arguments.get("query", "").strip()
    t_total = time.time()
    logger.info("[tool] search_info: query=%r", query)

    # Step 1 — RAG
    t0 = time.time()
    rag_results = await asyncio.to_thread(query_vectorstore, query, 8)
    # Re-rank: boost "Tòa X là của Y" pattern khi query hỏi vị trí
    rag_results = _rerank_for_location(query, rag_results)
    rag_useful = _is_rag_useful(query, rag_results)
    logger.info("[tool]   RAG: %d hits, useful=%s (%.2fs)",
                len(rag_results), rag_useful, time.time() - t0)

    payload: dict
    if rag_useful:
        payload = {
            "source": "rag",
            "hits": [
                {"content": r["content"][:800], "source_url": r["source_url"]}
                for r in rag_results
            ],
        }
    else:
        # Step 2 — Internet fallback (Tavily nếu có key, else DDG)
        t1 = time.time()
        try:
            raw, backend = await asyncio.to_thread(_internet_search, query, 2)
            net_hits = [
                {
                    "title": r.get("title", "")[:200],
                    "snippet": (r.get("body") or "")[:400],
                    "url": r.get("href", ""),
                }
                for r in raw
            ]
            logger.info("[tool]   Internet (%s): %d hits (%.2fs)", backend, len(net_hits), time.time() - t1)
            if net_hits:
                payload = {"source": "internet", "hits": net_hits}
            else:
                payload = {"source": "none", "hits": [], "note": "RAG không liên quan, Internet rỗng."}
        except Exception as e:
            logger.warning("[tool]   Internet error: %s", e)
            payload = {"source": "none", "hits": [], "error": str(e)[:200]}

    logger.info("[tool] search_info done %.2fs total, source=%s",
                time.time() - t_total, payload["source"])
    await params.result_callback(result=json.dumps(payload, ensure_ascii=False))


async def run_chatbot(user_message: str, conversation_history: list[dict]) -> dict:
    """
    Nhận một tin nhắn người dùng + lịch sử hội thoại,
    trả về dict kết quả (text hoặc navigation).

    Args:
        user_message:        Tin nhắn mới nhất của người dùng.
        conversation_history: Danh sách dict {"role": "user"/"assistant", "content": "..."}
                              đại diện cho các lượt trước (không bao gồm tin nhắn hiện tại).

    Returns:
        {"type": "text",       "content": "..."}
        {"type": "navigation", "content": {"event": ..., "destination_building": ...}}
    """
    api_key = os.getenv("LLM_API_KEY") or "ollama"
    llm = OpenAILLMService(
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        api_key=api_key,
        settings=OpenAILLMService.Settings(
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            max_tokens=1024,
            temperature=0.3,
        ),
    )
    system_prompt = await _build_system_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        *conversation_history,
    ]
    context = LLMContext(messages=messages, tools=tools_schema)
    aggregator_pair = LLMContextAggregatorPair(context)
    user_agg = aggregator_pair.user()
    assistant_agg = aggregator_pair.assistant()
    collector = OutputCollector(task=None, context=context)
    pipeline = Pipeline([user_agg, llm, assistant_agg, collector])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=False),
    )
    collector._task = task
    llm.register_function(
        "trigger_navigation",
        lambda p: handle_trigger_navigation(p, collector, task),
    )
    # search_info gộp RAG + Internet fallback. Có thể chain 2 tool nội bộ → bump timeout.
    llm.register_function(
        "search_info",
        handle_search_info,
        timeout_secs=60,
    )

    runner = PipelineRunner(handle_sigint=False)

    async def _push_message():
        context.add_message({"role": "user", "content": user_message})
        await task.queue_frame(LLMContextFrame(context=context))

    # Hard timeout: nếu pipeline không ra `text` hoặc `navigation` trong N giây
    # thì hủy task và trả response timeout.
    # 30s = cover được 2 LLM round-trip + search_info (RAG + Internet) + tail latency.
    # Internet search chậm (DDG ~2-5s, Tavily ~500ms) là bottleneck chính.
    import time
    timeout_secs = float(os.getenv("CHATBOT_TIMEOUT_SECS", "30"))
    t_start = time.time()
    logger.info("[chat] start: msg=%r", user_message[:80])
    try:
        await asyncio.wait_for(
            asyncio.gather(runner.run(task), _push_message()),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        logger.warning("[chat] TIMEOUT sau %.2fs (limit %.1fs)", time.time() - t_start, timeout_secs)
        try:
            await task.cancel()
        except Exception as e:
            logger.debug("task.cancel() ignored: %s", e)
        return {
            "type": "text",
            "content": (
                f"Mình chưa phản hồi kịp trong {int(timeout_secs)} giây. "
                "Bạn thử hỏi lại hoặc diễn đạt ngắn gọn hơn nhé."
            ),
        }

    result = collector.result
    logger.info("[chat] done %.2fs type=%s", time.time() - t_start, result.get("type"))
    return result
