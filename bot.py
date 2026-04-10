"""
bot.py — Pipecat chatbot pipeline cho trợ lý tòa nhà ĐHBK TP.HCM.

Luồng xử lý:
  UserMessage → LLMUserContextAggregator → OLLamaLLMService (local)
      → (text)  LLMAssistantContextAggregator → phản hồi văn bản
      → (tool)  trigger_navigation handler   → NavigationResult (JSON)

Yêu cầu:
  - Ollama đang chạy tại http://localhost:11434
  - Đã pull model hỗ trợ function calling:
      ollama pull qwen2.5:7b        (khuyên dùng — hỗ trợ tool tốt)
      ollama pull llama3.1:8b       (lựa chọn thay thế)
      ollama pull mistral-nemo      (nhanh, nhẹ)
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from database import db_get_all_buildings, db_get_building

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

# ── LLM service — hỗ trợ Ollama local hoặc cloud API (Groq, Together, v.v.) ──
# Dùng OpenAILLMService trực tiếp để có thể truyền api_key thật cho cloud.
# Nếu LLM_API_KEY trống → fallback sang "ollama" (cho Ollama local).
from pipecat.services.openai.llm import OpenAILLMService

# ── Function calling helpers ─────────────────────────────────────────────────
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ============================================================================
# 1. DANH SÁCH TÒA NHÀ — chỉ lấy tên/key cho system prompt (tiết kiệm token)
# ============================================================================

async def _build_buildings_index() -> str:
    """
    Chỉ lấy key + tên các tòa nhà để đưa vào system prompt (~10 token/tòa nhà).
    Chi tiết được fetch on-demand qua tool get_building_info khi LLM cần.
    """
    try:
        buildings = await db_get_all_buildings()
    except Exception as e:
        logger.warning("Không thể load buildings từ DB: %s", e)
        return ""

    if not buildings:
        return ""

    lines = ["## CÁC ĐỊA ĐIỂM TRONG KHUÔN VIÊN TRƯỜNG (dùng key để tra cứu chi tiết)\n"]
    for b in buildings:
        lines.append(f"- key=`{b.get('key')}` → {b.get('ten', b.get('key'))}")
    return "\n".join(lines)


# ============================================================================
# 2. SYSTEM PROMPT
# ============================================================================

_SYSTEM_PROMPT_TEMPLATE = """\
Bạn là trợ lý ảo thông minh của Đại học Bách Khoa TP.HCM (HCMUT / BK).
Nhiệm vụ của bạn là hỗ trợ sinh viên, giảng viên và khách thăm quan tìm hiểu \
thông tin về các tòa nhà trong khuôn viên trường.

{buildings_section}

## NGUYÊN TẮC SỬ DỤNG TOOL — CỰC KỲ QUAN TRỌNG

### NHÓM A — Câu hỏi thông tin về tòa nhà → GỌI `get_building_info` trước
Khi người dùng hỏi chi tiết về một địa điểm (mô tả, dịch vụ, số tầng, khoa...):
1. Gọi `get_building_info` với key tương ứng từ danh sách trên.
2. Dùng dữ liệu trả về để trả lời người dùng.
- Ví dụ: "Tòa A4 có những gì?" → get_building_info(key="A4")
- Ví dụ: "Thư viện có bao nhiêu sách?" → get_building_info(key="thu-vien")

### NHÓM B — Người dùng muốn đi đến một địa điểm → GỌI `trigger_navigation`
Từ khóa chỉ đường: "chỉ đường", "đi đến", "đường đến", "tới … đi đường nào",
"dẫn tôi đến", "làm sao đến", "muốn đến", "navigate", "directions"

Khi nhận ra ý định chỉ đường, bạn PHẢI:
1. Gọi ngay `trigger_navigation` với `destination_building` = tên địa điểm người dùng muốn đến.
2. KHÔNG viết thêm bất kỳ văn bản giải thích nào trước khi gọi tool.

## NGÔN NGỮ
Mặc định trả lời bằng tiếng Việt. Nếu người dùng hỏi bằng tiếng Anh, trả lời bằng tiếng Anh.

## PHONG CÁCH
Thân thiện, ngắn gọn, dùng bullet point khi liệt kê nhiều mục.
"""


async def _build_system_prompt() -> str:
    """Tạo system prompt với index tên tòa nhà (không nhét full data — tiết kiệm token)."""
    buildings_index = await _build_buildings_index()
    return _SYSTEM_PROMPT_TEMPLATE.format(buildings_section=buildings_index)


# ============================================================================
# 3. TOOL SCHEMA — trigger_navigation
# ============================================================================
#
# Pipecat dùng FunctionSchema để mô tả tool theo chuẩn JSON Schema,
# sau đó wrap vào ToolsSchema để truyền cho LLM service.
# Đây là cách khai báo PORTABLE (hoạt động với cả Anthropic, OpenAI, Gemini).
#
# Các trường bắt buộc:
#   name        — tên hàm, LLM sẽ dùng tên này để gọi
#   description — mô tả RÕ RÀNG để LLM biết khi nào cần gọi
#   properties  — dict[tên_tham_số → {type, description}]
#   required    — danh sách tham số bắt buộc
# ============================================================================

navigation_tool = FunctionSchema(
    name="trigger_navigation",
    description=(
        "Gọi hàm này KHI VÀ CHỈ KHI người dùng muốn được chỉ đường hoặc "
        "điều hướng đến một tòa nhà/địa điểm cụ thể trong khuôn viên trường. "
        "Không gọi hàm này cho các câu hỏi thông tin thông thường."
    ),
    properties={
        "destination_building": {
            "type": "string",
            "description": (
                "Tên địa điểm/tòa nhà mà người dùng muốn đến. "
                "Ví dụ: 'Tòa A4', 'Thư viện Trung tâm', 'Ký túc xá', 'Hội trường A'. "
                "Giữ nguyên tên như người dùng nêu, có thể thêm 'Tòa' nếu thiếu."
            ),
        }
    },
    required=["destination_building"],
)

building_info_tool = FunctionSchema(
    name="get_building_info",
    description=(
        "Tra cứu thông tin chi tiết của một tòa nhà/địa điểm theo key. "
        "Gọi hàm này khi người dùng hỏi về mô tả, dịch vụ, số tầng, khoa/đơn vị của một địa điểm cụ thể. "
        "Dùng key chính xác từ danh sách địa điểm trong system prompt."
    ),
    properties={
        "key": {
            "type": "string",
            "description": "Key định danh của tòa nhà, ví dụ: 'A4', 'thu-vien', 'ky-tuc-xa'.",
        }
    },
    required=["key"],
)

tools_schema = ToolsSchema(standard_tools=[navigation_tool, building_info_tool])


# ============================================================================
# 4. KẾT QUẢ ĐIỀU HƯỚNG (data class dùng như signal giữa pipeline và API)
# ============================================================================

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


# ============================================================================
# 5. FRAME COLLECTOR — thu thập output từ pipeline
# ============================================================================

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
            # Kiểm tra message cuối của assistant:
            # - Nếu là tool call (content rỗng, có tool_calls) → LLM sẽ chạy lại sau khi tool trả kết quả, chưa end.
            # - Nếu là text thật → end pipeline.
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
        # Lấy assistant message cuối cùng từ context
        text = ""
        if self._context:
            messages = self._context.messages
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    text = msg.get("content") or ""
                    break
        return {"type": "text", "content": text.strip()}


# ============================================================================
# 6. FUNCTION HANDLER — xử lý khi LLM gọi trigger_navigation
# ============================================================================

async def handle_trigger_navigation(
    params: FunctionCallParams,
    collector: OutputCollector,
    task: PipelineTask,
) -> None:
    """
    Handler được Pipecat gọi tự động khi LLM phát ra tool_use block
    với name = "trigger_navigation".

    Tham số nhận được qua params.arguments (dict, đã parse từ JSON):
      - destination_building: str

    Luồng xử lý:
      1. Trích xuất destination_building từ arguments.
      2. Log ra console để debug/audit.
      3. Lưu NavigationResult vào collector để API layer lấy về.
      4. Trả kết quả cho LLM qua result_callback (bắt buộc để pipeline
         không bị treo — Pipecat cần biết tool đã hoàn thành).
      5. Đẩy EndFrame để kết thúc sớm pipeline (không cần LLM sinh thêm text).
    """
    destination = params.arguments.get("destination_building", "Không xác định")

    # ── Bước 1: Log ─────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("🧭 NAVIGATION TRIGGERED")
    logger.info("   destination_building = %s", destination)
    logger.info("=" * 60)

    # ── Bước 2: Lưu kết quả vào collector ───────────────────────────────────
    collector.navigation = NavigationResult(destination=destination)

    # ── Bước 3: Trả kết quả cho LLM (bắt buộc) ──────────────────────────────
    # result_callback đưa kết quả vào context dưới dạng tool_result message.
    # Truyền run_llm=False để LLM không sinh thêm phản hồi văn bản sau tool.
    # API v0.0.105+: run_llm truyền qua FunctionCallResultProperties, không phải kwarg trực tiếp
    await params.result_callback(
        result=json.dumps({"status": "navigation_triggered", "destination": destination}),
        properties=FunctionCallResultProperties(run_llm=False),
    )

    # ── Bước 4: Kết thúc pipeline ────────────────────────────────────────────
    await task.queue_frame(EndFrame())


# ============================================================================
# 6b. FUNCTION HANDLER — get_building_info (tra cứu chi tiết on-demand)
# ============================================================================

async def handle_get_building_info(params: FunctionCallParams) -> None:
    """
    Handler khi LLM cần chi tiết một tòa nhà.
    Fetch từ DB → trả JSON về cho LLM → LLM dùng data đó để trả lời người dùng.
    run_llm mặc định = True (LLM tiếp tục sinh câu trả lời sau khi có data).
    """
    key = params.arguments.get("key", "").strip()
    logger.info("get_building_info: key=%s", key)

    building = await db_get_building(key)
    if building:
        result = json.dumps(building, ensure_ascii=False)
    else:
        result = json.dumps({"error": f"Không tìm thấy địa điểm với key='{key}'"})

    await params.result_callback(result=result)


# ============================================================================
# 7. HÀM CHÍNH — khởi tạo và chạy pipeline
# ============================================================================

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
    # ── LLM service ──────────────────────────────────────────────────────────
    # Cấu hình qua .env:
    #   OLLAMA_BASE_URL  — endpoint (Ollama: http://localhost:11434/v1 | Groq: https://api.groq.com/openai/v1)
    #   OLLAMA_MODEL     — tên model  (Ollama: qwen2.5:7b | Groq: llama-3.3-70b-versatile)
    #   LLM_API_KEY      — bỏ trống cho Ollama local; điền key cho Groq/Together/v.v.
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

    # ── Context + tools ──────────────────────────────────────────────────────
    # API mới (v0.0.105+): dùng LLMContext phổ quát, tools gắn vào context.
    # System prompt được build động từ DB mỗi request.
    system_prompt = await _build_system_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        *conversation_history,
    ]
    context = LLMContext(messages=messages, tools=tools_schema)
    aggregator_pair = LLMContextAggregatorPair(context)
    user_agg = aggregator_pair.user()
    assistant_agg = aggregator_pair.assistant()

    # ── Pipeline ─────────────────────────────────────────────────────────────
    # Luồng frame: user_agg → llm → assistant_agg → collector
    # collector._task được gán sau khi task được tạo bên dưới.
    collector = OutputCollector(task=None, context=context)
    pipeline = Pipeline([user_agg, llm, assistant_agg, collector])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=False),
    )
    collector._task = task

    # ── Đăng ký tool handler ─────────────────────────────────────────────────
    # register_function(name, handler):
    #   - name:    phải khớp chính xác với FunctionSchema.name ở trên
    #   - handler: async callable nhận FunctionCallParams
    # Pipecat tự động gọi handler này khi LLM phát ra tool_use block.
    llm.register_function(
        "trigger_navigation",
        lambda p: handle_trigger_navigation(p, collector, task),
    )
    llm.register_function(
        "get_building_info",
        handle_get_building_info,
    )

    # ── Khởi động pipeline và đẩy tin nhắn đầu vào ──────────────────────────
    runner = PipelineRunner(handle_sigint=False)

    async def _push_message():
        # Thêm tin nhắn user vào context rồi gửi LLMContextFrame để kích hoạt LLM
        context.add_message({"role": "user", "content": user_message})
        await task.queue_frame(LLMContextFrame(context=context))
        # EndFrame sẽ được gửi sau khi pipeline hoàn thành (từ handler hoặc LLM xong)

    await asyncio.gather(
        runner.run(task),
        _push_message(),
    )

    return collector.result


# ============================================================================
# 8. ENTRY POINT — chạy thử trực tiếp
# ============================================================================

async def _demo():
    """Demo nhanh, không cần server."""
    test_cases = [
        "Tòa A4 có những phòng thí nghiệm gì?",
        "Chỉ tôi đường đến thư viện",
        "Thư viện có bao nhiêu đầu sách?",
        "Tới ký túc xá đi đường nào?",
        "Hội trường A sức chứa bao nhiêu người?",
        "Dẫn tôi tới tòa B4",
    ]

    history: list[dict] = []

    for msg in test_cases:
        print(f"\n{'─'*60}")
        print(f"👤 User: {msg}")
        result = await run_chatbot(msg, history)

        if result["type"] == "text":
            print(f"🤖 Bot : {result['content']}")
            # Cập nhật history để giữ ngữ cảnh hội thoại
            history.append({"role": "user", "content": msg})
            history.append({"role": "assistant", "content": result["content"]})
        else:
            nav = result["content"]
            print(f"🧭 NAVIGATION EVENT: {json.dumps(nav, ensure_ascii=False, indent=2)}")
            # Không thêm vào history — navigation kết thúc luồng

    print(f"\n{'─'*60}")
    print("Demo hoàn tất.")


async def _chat():
    """Chat tương tác trực tiếp trên terminal."""
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   Trợ lý tòa nhà ĐHBK TP.HCM  —  gõ 'quit' để thoát   ║")
    print("╚══════════════════════════════════════════════════════════╝")

    history: list[dict] = []

    while True:
        try:
            user_input = input("\n👤 Bạn: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nTạm biệt!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "thoát"):
            print("Tạm biệt!")
            break

        result = await run_chatbot(user_input, history)

        if result["type"] == "text":
            print(f"🤖 Bot: {result['content']}")
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": result["content"]})
        else:
            dest = result["content"]["destination_building"]
            print(f"🧭 [Điều hướng] → {dest}")
            # Không lưu navigation vào history


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        asyncio.run(_demo())
    else:
        asyncio.run(_chat())
