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
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

async def _build_buildings_index() -> str:
    """
    Chỉ lấy key + tên các tòa nhà để đưa vào system prompt.
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
    """Tạo system prompt với index tên tòa nhà."""
    buildings_index = await _build_buildings_index()
    return _SYSTEM_PROMPT_TEMPLATE.format(buildings_section=buildings_index)

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


async def handle_trigger_navigation(
    params: FunctionCallParams,
    collector: OutputCollector,
    task: PipelineTask,
) -> None:
    destination = params.arguments.get("destination_building", "Không xác định")
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


async def handle_get_building_info(params: FunctionCallParams) -> None:
    """
    Handler khi LLM cần chi tiết một tòa nhà.
    Fetch từ DB → trả JSON về cho LLM → LLM dùng data đó để trả lời người dùng.
    run_llm mặc định = True.
    """
    key = params.arguments.get("key", "").strip()
    logger.info("get_building_info: key=%s", key)

    building = await db_get_building(key)
    if building:
        result = json.dumps(building, ensure_ascii=False)
    else:
        result = json.dumps({"error": f"Không tìm thấy địa điểm với key='{key}'"})

    await params.result_callback(result=result)


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
    llm.register_function(
        "get_building_info",
        handle_get_building_info,
    )

    runner = PipelineRunner(handle_sigint=False)

    async def _push_message():
        context.add_message({"role": "user", "content": user_message})
        await task.queue_frame(LLMContextFrame(context=context))

    await asyncio.gather(
        runner.run(task),
        _push_message(),
    )

    return collector.result
