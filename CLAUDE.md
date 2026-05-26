# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Pipecat-based chatbot API that serves as a virtual assistant for HCMUT (Ho Chi Minh University of Technology). Uses **OpenAI-compatible LLM** (DeepSeek default) with function calling. Building info + HCMUT knowledge stored as **markdown/txt files** indexed via **RAG** (LangChain + Chroma + Jina v3 API embeddings). Web search fallback via DuckDuckGo. Exposed as REST API for C# ASP.NET Core backend (CO4029_BE).

**Không còn dùng MongoDB.** Building data nằm trong `docs/buildings/*.md`, được index cùng knowledge base và truy cập qua tool `search_knowledge`.

## File Structure

```
api.py                  — FastAPI server (POST /chat + docs router + serves frontend; warmup + auto-rebuild RAG ở startup)
bot.py                  — Pipecat pipeline, 3 tools, system prompt, run_chatbot()
auth.py                 — API key verification (X-API-Key header)
routers/docs.py         — Knowledge base CRUD (upload/list/delete/rebuild)
formatter.py            — Convert markdown → TMP rich text / plain (cho /chat?format=tmp|plain)
rag.py                  — LangChain RAG: load docs/ rglob → Jina v3 API embed → Chroma persistent
scripts/crawl.py        — Playwright crawler → docs/ (cho SPA — chạy LOCAL khi cần refresh)
scripts/build_index.py  — Chạy sau crawl/sửa docs: index docs/ → chroma_db/ (alternative: API /docs/rebuild)
docs/                   — Markdown knowledge base. KHÔNG có file preset — user tự upload qua UI.
docs/buildings/         — (optional) Building docs với frontmatter building_key+building_name → tự đẩy vào catalog
docs/uploaded/          — Files user upload qua /docs/upload endpoint
chroma_db/              — Chroma persistent vector store (gitignored sau khi index)
frontend/               — Vue 3 + Vite dashboard (Chat Test + Knowledge Base tabs)
dist/                   — Frontend build output (gitignored, sinh ra khi build)
Procfile / runtime.txt  — Cấu hình deploy (Render/Railway): start command + Python version
render.yaml             — Render Blueprint config (build + start + env vars)
DEPLOY.md               — Hướng dẫn deploy lên Render
.env                    — Biến môi trường thật (KHÔNG commit). Set: OLLAMA_BASE_URL, OLLAMA_MODEL, LLM_API_KEY, JINA_API_KEY, CHATBOT_API_KEY, CHATBOT_ALLOWED_ORIGINS, CHATBOT_TIMEOUT_SECS, (optional) TAVILY_API_KEY
```

Không có test suite (pytest/unittest). Verify thay đổi qua `python bot.py demo` hoặc gọi `/chat` trực tiếp.

## Setup

```bash
# 1. Cài Python dependencies
python -m venv .venv

# Linux/Mac:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# 2. Tạo .env (xem env vars cần thiết ở phần File Structure ở trên)

# 3. Crawl + index knowledge base (chạy 1 lần khi setup hoặc khi muốn refresh)
.venv/bin/python -m playwright install chromium   # ~280MB, lần đầu
.venv/bin/python scripts/crawl.py                   # Playwright crawl ~14 SPA route → docs/*.md
.venv/bin/python scripts/build_index.py                   # docs/ + docs/buildings/ → chroma_db/

# 4. Chạy server — LUÔN dùng uvicorn trong venv
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --reload
# Windows: .venv\Scripts\uvicorn api:app --host 0.0.0.0 --port 8000 --reload
# Hoặc: python api.py

# 5. Debug pipeline KHÔNG cần HTTP (chạy thẳng bot.py):
python bot.py          # chat tương tác trên terminal
python bot.py demo     # chạy chuỗi câu test có sẵn (tool calls + text)
```

**Thêm/sửa tòa nhà**: tạo/sửa file `docs/buildings/{key}.md` với frontmatter `building_key`, `building_name`, `khoa`, `tang` → chạy lại `scripts/build_index.py`.

## Development & Testing

```bash
# Chạy backend API (api.py warmup embeddings ở startup event)
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# Chạy frontend dev server (hot reload, proxy /chat → localhost:8000)
cd frontend && npm run dev
```

Dùng dashboard tại `http://localhost:5173` để test chat.
Khi deploy, frontend được build vào `dist/` và FastAPI tự serve nó tại `/`.

## LLM Backend — cấu hình qua .env

Hỗ trợ Ollama local hoặc bất kỳ OpenAI-compatible cloud API (Groq, Together, v.v.).
Chỉ cần đổi 3 biến trong `.env`, không sửa code:

```env
# Groq (cloud, miễn phí, ~300 tok/s — khuyên dùng cho production):
OLLAMA_BASE_URL=https://api.groq.com/openai/v1
OLLAMA_MODEL=openai/gpt-oss-20b    # tool calling ổn định nhất với 3+ tools
LLM_API_KEY=gsk_xxxx               # lấy tại console.groq.com

# Ollama local (không cần key):
# OLLAMA_BASE_URL=http://localhost:11434/v1
# OLLAMA_MODEL=qwen2.5:7b
# LLM_API_KEY=
```

Nếu `LLM_API_KEY` trống → dùng `"ollama"` làm api_key (Ollama local không cần key thật).

**Model choice cho tool calling** (Groq): `llama-3.3-70b-versatile` đôi khi xuất tool call ở format Llama legacy `<function=name{args}</function>` → Groq parser reject ("Failed to call a function"). Dùng `openai/gpt-oss-20b` (OpenAI strict tool format) hoặc `meta-llama/llama-4-scout-17b-16e-instruct` thay thế.

## Architecture

```
POST /chat
    │
    ▼
run_chatbot(message, history)
    │
    ├── _build_system_prompt()
    │       └── _build_buildings_index()  ← đọc docs/buildings/*.md frontmatter (sync)
    │
    ▼
LLMContextAggregatorPair (user + assistant)
    │
    ▼
OpenAILLMService  ← Groq hoặc Ollama, cùng API
    │
    ├── tool_use: trigger_navigation(destination_building)   [LUỒNG 1]
    │       └── Code guardrail (reject khoa/phòng) → NavigationResult → EndFrame
    │
    ├── tool_use: search_info(query)   [LUỒNG 2 — auto RAG → Internet fallback]
    │       ├── Step 1: query_vectorstore (Chroma hybrid BM25+semantic, k=8)
    │       ├── Step 2: _is_rag_useful() — heuristic match >=50% query tokens
    │       ├── Step 3a: useful → return source="rag" + hits
    │       └── Step 3b: not useful → DDG search → source="internet" + hits
    │                  (nếu cả 2 fail → source="none")
    │
    └── text response   [LUỒNG 3 — fallback khi search_info source=none]
            └── LLM trả "chưa tìm được thông tin"
```

**RAG pipeline** (offline, chạy 1 lần):
```
scripts/crawl.py (Playwright headless Chromium, vì hcmut.edu.vn là SPA)
    → docs/*.md (YAML frontmatter: source_url, crawled_at)
docs/buildings/*.md (optional — frontmatter: building_key, building_name, khoa, tang)
docs/uploaded/*.md (user upload qua UI hoặc API /docs/upload)
scripts/build_index.py / POST /docs/rebuild → rag.build_vectorstore()
    → Path.rglob (đệ quy, bao gồm cả subdirs)
    → RecursiveCharacterTextSplitter (chunk=500, overlap=50)
    → JinaEmbeddings (jina-embeddings-v3 API, 1024D, multilingual, batch 32/request)
    → Chroma persistent → chroma_db/
```

**Embedding**: Jina v3 API (`https://api.jina.ai/v1/embeddings`) — multilingual bi-encoder MTEB top tier. Lý do dùng API thay vì local model: Render free tier 512MB RAM không đủ cho HF model (~540MB). Free 1M token/tháng. **Đổi provider/dim = phải re-index** — xóa `chroma_db/` rồi chạy `scripts/build_index.py`. Task mode: `retrieval.passage` cho docs, `retrieval.query` cho query (asymmetric retrieval).

**Hybrid retrieval** (`rag.query_vectorstore`): BM25 keyword + dense embedding semantic,
merge bằng Reciprocal Rank Fusion (RRF). Cần thiết cho tiếng Việt vì embedding
multilingual MiniLM yếu khi match keyword như "Khoa Cơ khí" với câu chứa cả "Khoa" và
"Cơ khí" rời nhau. BM25 (rank_bm25) bù điểm yếu này.

Tại request time: `search_knowledge` handler gọi `rag.query_vectorstore(query, k=8)` →
chạy trong `asyncio.to_thread` (vì Chroma sync) → trả top-8 chunks ≤800 ký tự kèm `source_url` → LLM tổng hợp.

`api.py` có `@app.on_event("startup")` warmup gọi `query_vectorstore("warmup", 1)` để verify Jina API key + Chroma connection trước khi nhận traffic. Nếu chroma_db/ chưa có thì tự rebuild từ docs/.

**3 luồng xử lý** (chỉ 2 tool, fallback do code orchestrate):
1. **LUỒNG 1**: User yêu cầu dẫn đường → `trigger_navigation`. Pattern: "chỉ đường", "dẫn tôi", "đi đến X làm sao". Có guardrail code-level reject khi destination là khoa/phòng/tiện ích (chứa "khoa ", "phòng ", "canteen", etc.) hoặc format phòng (`F1.05`, `A205`).
2. **LUỒNG 2**: Mọi câu hỏi thông tin → `search_info(query)`. Handler tự động:
   - RAG hybrid retrieval (k=8)
   - Đánh giá `_is_rag_useful()` qua keyword overlap heuristic (>=50% tokens query xuất hiện trong top-3 hits)
   - Nếu useful → trả source="rag"; không useful → auto fallback DuckDuckGo, source="internet"
   - Cả 2 fail → source="none"
3. **LUỒNG 3**: search_info trả source="none" → LLM trả TEXT "chưa tìm được thông tin"

LLM không cần tự quyết "khi nào RAG, khi nào internet" — code handler tự xử lý. Giảm khả năng LLM "lười" stop ở RAG khi RAG không có data.

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

## Knowledge Base Management

Có 2 cách thêm knowledge:

### A. Upload qua UI/API (recommended)
- **UI**: Mở dashboard → tab **Knowledge Base** → kéo-thả file `.md`/`.txt`/`.pdf` (tối đa 20MB)
- **API**: `POST /docs/upload` (multipart, header `X-API-Key`), tự rebuild background sau upload
- File lưu vào `docs/uploaded/`, được index cùng các docs khác
- **Giữ extension gốc**: `.txt` → `.txt`, `.md` → `.md`, `.pdf` → extract text → `.txt` (vì PDF binary không index được)
- **Boost retrieval**: chunks từ `docs/uploaded/` được boost ×2.5 RRF score (user-curated ưu tiên hơn crawled noise)
- **Link penalty**: chunks chứa >40% markdown links / URLs bị giảm ×0.4 (tránh Drive link spam từ crawled docs lên top)

### B. Drop file vào `docs/` rồi rebuild index
- Bất kỳ `.md` nào trong `docs/` (đệ quy) đều được index
- Chạy `python scripts/build_index.py` sau khi thêm/sửa

### Building catalog (optional)
Nếu muốn có "DANH BẠ TÒA NHÀ" trong system prompt để LLM trigger_navigation ngay không cần search, tạo file trong `docs/buildings/{key}.md` với frontmatter:

```markdown
---
building_key: A4
building_name: Tòa A4
khoa: Đào tạo quốc tế (OISP)
tang: 5
---

(content tùy ý — sẽ được RAG index như doc khác)
```

`bot._build_buildings_index()` quét folder này, đẩy `name + khoa + tang` vào system prompt. Để trống folder = catalog rỗng, mọi nav phải qua RAG/internet để xác định tòa.

### Endpoints

| Method | Endpoint | Auth | Mô tả |
|---|---|---|---|
| GET | `/docs` | Không | List file trong `docs/uploaded/` |
| POST | `/docs/upload` | X-API-Key | Upload .md/.txt/.pdf, auto rebuild |
| POST | `/docs/crawl-url` | X-API-Key | Fetch URL → extract content → save .md → rebuild. Static HTML only (SPA báo 422). |
| DELETE | `/docs/{filename}` | X-API-Key | Xóa file đã upload |
| POST | `/docs/rebuild` | X-API-Key | Trigger rebuild manual (background) |
| GET | `/docs/rebuild/status` | Không | Trạng thái rebuild gần nhất |

`crawl-url` body: `{"url": "https://...", "rebuild": true}`. Dùng `requests + BeautifulSoup + html2text` (sync, ~1-2s/URL). KHÔNG render JS → cho SPA, dùng `scripts/crawl.py` (Playwright) local rồi upload kết quả.

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

// Để Unity render text → set ?format=tmp (TMP rich text)
// Để web render markdown → ?format=md (default)
// Để client text-only → ?format=plain
var response = await _httpClient.PostAsJsonAsync(
    $"{baseUrl}/chat?format=tmp",
    new { message = userMsg, history = previousMessages }
);

var result = await response.Content.ReadFromJsonAsync<JsonElement>();
if (result.GetProperty("type").GetString() == "navigation")
    var dest = result.GetProperty("content").GetProperty("destination_building").GetString();
else
    var text = result.GetProperty("content").GetString();  // text với TMP tags
```

### Response format options

| `?format=` | Output | Use case |
|---|---|---|
| `md` (default) | `**bold** *italic* [link](url) - bullet` | Frontend web (marked.js render) |
| `tmp` | `<b>bold</b> <i>italic</i> <link="url"><color=#2563eb><u>...</u></color></link> • bullet` | Unity TextMeshPro |
| `plain` | `bold italic link bullet` | Client text-only |

Navigation response (`type=navigation`) KHÔNG bị convert — `content` vẫn là dict `{event, destination_building}`.

Converter ở [formatter.py](formatter.py) — regex-based, handle 80% common markdown. Edge case phức tạp (nested formatting, tables) có thể không hoàn hảo.

## Extending

- **Thêm tòa nhà**: tạo `docs/buildings/{key}.md` với frontmatter → chạy lại `scripts/build_index.py`
- **Thêm knowledge mới**: paste markdown vào `docs/` (hoặc subfolder) → re-index
- **Thêm tool mới**: thêm `FunctionSchema` vào `tools_schema`, `register_function` trong `run_chatbot()`
  - Bump `timeout_secs` nếu tool gọi external service chậm (mặc định Pipecat = 10s)
- **Đổi model**: sửa `OLLAMA_MODEL` trong `.env`
- **Deploy**: `render.yaml` đã cấu hình sẵn — push lên Render, set env vars trong dashboard, deploy
- **Thêm frontend route**: thêm endpoint vào proxy list trong `frontend/vite.config.js`

## Important Notes

- **LUÔN chạy uvicorn qua venv** — system uvicorn thiếu packages
- **KHÔNG commit `.env`** — chứa key thật. Gitignore đã có sẵn
- Groq key bị lộ cần revoke ngay tại console.groq.com và tạo key mới
- Jina API embedding (~200ms/call) — không có model load local, fit Render free tier 512MB RAM
- **Request hard timeout**: `run_chatbot()` wrap `asyncio.wait_for(timeout=CHATBOT_TIMEOUT_SECS)` (default 30s, env override). Hết giờ → cancel pipeline + return text "Mình chưa phản hồi kịp trong N giây".
- **Internet search backend** (search_info fallback): auto-pick `TAVILY_API_KEY` nếu set (~500ms, sạch hơn) else DuckDuckGo qua `ddgs` (~1.5-3s). Tavily free tier 1000 req/tháng tại tavily.com.
