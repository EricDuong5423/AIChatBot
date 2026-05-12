# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Pipecat-based chatbot API that serves as a virtual assistant for buildings at HCMUT (Ho Chi Minh University of Technology). Uses **OpenAI-compatible LLM** (Groq cloud hoặc Ollama local) with function calling. Building data stored in **MongoDB**. Exposed as REST API for C# ASP.NET Core backend (CO4029_BE).

## File Structure

```
api.py                  — FastAPI server (chat + buildings CRUD + serves frontend)
bot.py                  — Pipecat pipeline, tool schemas, run_chatbot()
auth.py                 — API key verification (dùng chung cho api.py và routers)
database.py             — Motor async MongoDB client + CRUD functions
routers/buildings.py    — FastAPI router: GET/POST/PUT/DELETE /buildings
seed_buildings.py       — Script đổ dữ liệu mẫu vào MongoDB (chạy 1 lần)
frontend/               — Vue 3 + Vite dashboard (Chat Test + Buildings CRUD)
dist/                   — Frontend build output (gitignored, sinh ra khi build)
render.yaml             — Render deployment config (build + start commands)
.env                    — Biến môi trường thật (KHÔNG commit)
.env.example            — Template placeholder (commit được)
```

## Setup

```bash
# 1. Cài Python dependencies
python -m venv .venv

# Linux/Mac:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# 2. Cấu hình môi trường
cp .env.example .env
# Chỉnh .env: LLM backend, MongoDB URI, API keys

# 3. Đổ dữ liệu mẫu vào MongoDB (chạy 1 lần)
python seed_buildings.py

# 4. Chạy server — LUÔN dùng uvicorn trong venv
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --reload
# Windows: .venv\Scripts\uvicorn api:app --host 0.0.0.0 --port 8000 --reload
# Hoặc: python api.py
```

## Development & Testing

```bash
# Chạy backend API
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# Chạy frontend dev server (hot reload, proxy /chat và /buildings → localhost:8000)
cd frontend && npm run dev
```

Dùng dashboard tại `http://localhost:5173` để test chat và CRUD buildings.
Khi deploy, frontend được build vào `dist/` và FastAPI tự serve nó tại `/`.

## LLM Backend — cấu hình qua .env

Hỗ trợ Ollama local hoặc bất kỳ OpenAI-compatible cloud API (Groq, Together, v.v.).
Chỉ cần đổi 3 biến trong `.env`, không sửa code:

```env
# Groq (cloud, miễn phí, ~300 tok/s — khuyên dùng cho production):
OLLAMA_BASE_URL=https://api.groq.com/openai/v1
OLLAMA_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=gsk_xxxx           # lấy tại console.groq.com

# Ollama local (không cần key):
# OLLAMA_BASE_URL=http://localhost:11434/v1
# OLLAMA_MODEL=qwen2.5:7b
# LLM_API_KEY=
```

Nếu `LLM_API_KEY` trống → dùng `"ollama"` làm api_key (Ollama local không cần key thật).

## Architecture

```
POST /chat
    │
    ▼
run_chatbot(message, history)
    │
    ├── _build_system_prompt()
    │       └── _build_buildings_index()  ← chỉ lấy key+tên từ DB (~10 token/building)
    │
    ▼
LLMContextAggregatorPair (user + assistant)
    │
    ▼
OpenAILLMService  ← Groq hoặc Ollama, cùng API
    │
    ├── tool_use: get_building_info(key)
    │       └── db_get_building(key) → JSON chi tiết → LLM dùng để trả lời
    │
    ├── tool_use: trigger_navigation(destination_building)
    │       └── NavigationResult → collector.navigation → EndFrame
    │
    └── text response
            └── LLMContextAssistantTimestampFrame → đọc từ context → EndFrame
```

**Token optimization**: System prompt chỉ chứa index tên tòa nhà. Chi tiết được fetch on-demand qua tool `get_building_info` khi LLM cần → tiết kiệm ~80% token so với nhét full data vào prompt.

**OutputCollector** (`bot.py`): gửi `EndFrame` khi `LLMContextAssistantTimestampFrame` fire VÀ message cuối không phải tool call (tránh tắt pipeline sớm khi LLM đang chờ kết quả tool).

**Response format** của `run_chatbot()`:
```python
{"type": "text",       "content": "Tòa A4 có 8 tầng..."}
{"type": "navigation", "content": {"event": "navigation_triggered", "destination_building": "library"}}
```

## Tool Declaration Pattern

```python
# 1. Define schema — tools gắn vào LLMContext, KHÔNG set trên llm service
tool = FunctionSchema(name="my_tool", description="...", properties={...}, required=[...])
tools_schema = ToolsSchema(standard_tools=[tool])
context = LLMContext(messages=messages, tools=tools_schema)

# 2. Register handler BEFORE running pipeline
llm.register_function("my_tool", handler_coroutine)

# 3. Handler phải luôn gọi result_callback — pipeline treo nếu không gọi
async def handler(params: FunctionCallParams):
    await params.result_callback(
        result=json.dumps({...}),
        properties=FunctionCallResultProperties(run_llm=False),  # False = không sinh text sau tool
    )
```

## Buildings CRUD API

| Method | Endpoint | Auth | Mô tả |
|---|---|---|---|
| GET | `/buildings` | Không | Liệt kê tất cả |
| GET | `/buildings/{key}` | Không | Lấy theo key |
| POST | `/buildings` | X-API-Key | Tạo mới |
| PUT | `/buildings/{key}` | X-API-Key | Cập nhật |
| DELETE | `/buildings/{key}` | X-API-Key | Xóa |

**Building schema** (MongoDB document):
```json
{
  "key": "A4",
  "ten": "Tòa nhà A4",
  "mo_ta": "Mô tả chi tiết...",
  "khoa": "Khoa Điện-Điện tử",
  "tang": 8,
  "dich_vu": ["Phòng học", "Phòng thí nghiệm"]
}
```

## Security

- `CHATBOT_API_KEY` — tự đặt, dùng `openssl rand -hex 32` để tạo. C# backend gửi kèm header `X-API-Key`.
- Nếu để trống → không kiểm tra (OK cho dev local).
- `auth.py` chứa `verify_api_key` dùng chung — import từ đây, không duplicate.

## C# Backend Integration (CO4029_BE)

```csharp
// appsettings.json
"ChatbotApi": {
  "BaseUrl": "http://localhost:8000",
  "ApiKey": "your_secret_key_here"
}

// ChatbotService.cs
_httpClient.DefaultRequestHeaders.Add("X-API-Key", configuration["ChatbotApi:ApiKey"]);

// Response handling
var result = await response.Content.ReadFromJsonAsync<JsonElement>();
if (result.GetProperty("type").GetString() == "navigation")
    var dest = result.GetProperty("content").GetProperty("destination_building").GetString();
else
    var text = result.GetProperty("content").GetString();
```

## Extending

- **Thêm tòa nhà**: dùng `POST /buildings` API hoặc thêm vào `seed_buildings.py`
- **Thêm tool mới**: thêm `FunctionSchema` vào `tools_schema`, `register_function` trong `run_chatbot()`
- **Đổi model**: sửa `OLLAMA_MODEL` trong `.env`
- **Deploy**: `render.yaml` đã cấu hình sẵn — push lên Render, set env vars trong dashboard, deploy
- **Thêm frontend route**: thêm endpoint vào proxy list trong `frontend/vite.config.js`

## Important Notes

- **LUÔN chạy uvicorn qua venv** — system uvicorn thiếu packages
- **KHÔNG commit `.env`** — chứa key thật; chỉ commit `.env.example`
- Groq key bị lộ cần revoke ngay tại console.groq.com và tạo key mới
