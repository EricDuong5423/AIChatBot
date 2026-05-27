# HCMUT Chatbot — Kiến trúc & Luồng hoạt động

Tài liệu này giải thích chi tiết cách chatbot HCMUT hoạt động: kiến trúc tổng thể, các thành phần cốt lõi, luồng xử lý 1 câu hỏi, và các quyết định thiết kế.

> **Mục tiêu chatbot**: trợ lý ảo HCMUT có thể (1) trả lời câu hỏi về trường, (2) chỉ đường tới tòa nhà, (3) tìm trên internet khi không có thông tin nội bộ. Output có thể hiển thị trên web (markdown), Unity (TMP rich text), hoặc plain text.

---

## 1. Tổng quan tính năng

| Tính năng | Mô tả |
|---|---|
| **Chat hỏi đáp** | Trả lời câu hỏi về HCMUT (chương trình đào tạo, tòa nhà, học phí…) dựa trên knowledge base |
| **Trigger Navigation** | Khi user yêu cầu dẫn đường, trả về `{event, destination_building}` để frontend hoặc Unity điều hướng |
| **Internet Fallback** | Tự động search DuckDuckGo/Tavily khi knowledge base không có thông tin |
| **Knowledge Base Management** | Upload `.md` / `.txt` / `.pdf` qua web UI hoặc crawl từ URL |
| **Multi-format Output** | Markdown (web), TMP rich text (Unity), plain text |
| **Hard Timeout** | Tự cancel + fallback text nếu request quá 30s |

---

## 2. Tech Stack

| Layer | Công nghệ | Vai trò |
|---|---|---|
| **LLM** | DeepSeek (`deepseek-chat`) qua OpenAI-compatible API | Reasoning + tool calling + text generation |
| **Pipeline orchestration** | Pipecat | Quản lý dialog flow, tool calling, streaming |
| **Vector DB** | Chroma (persistent SQLite + HNSW index) | Lưu vector embeddings, similarity search |
| **Embedding** | Jina v3 API (`jina-embeddings-v3`, 1024D, multilingual) | Vector hóa text qua REST API — 0 RAM local, free 1M token/tháng |
| **Sparse retriever** | BM25Okapi (`rank_bm25`) | Keyword matching, bù điểm yếu của semantic |
| **Internet search** | DuckDuckGo qua `ddgs` (default) hoặc Tavily | Fallback khi RAG không có data |
| **Backend** | FastAPI + Uvicorn | HTTP server, REST API |
| **Frontend** | Vue 3 + Vite + TailwindCSS + marked.js | Dashboard chat + knowledge base management |
| **Crawler** | Playwright headless Chromium ([scripts/crawl.py](scripts/crawl.py)) | Crawl SPA site khi cần (chạy LOCAL, không deploy) |
| **PDF extraction** | `pypdf` | Convert PDF → text khi upload |
| **Deploy** | Render | Single web service, free tier |

---

## 3. Kiến trúc tổng thể

```mermaid
graph TB
    subgraph Client["Client Layer"]
        WEB["Web Dashboard<br/>Vue + marked.js"]
        UNITY["Unity App<br/>TextMeshPro"]
        CSHARP["C# Backend<br/>CO4029_BE"]
    end

    subgraph API["FastAPI Server"]
        CHAT["POST /chat"]
        DOCS["/docs/* upload, list, rebuild"]
        HEALTH["/health"]
    end

    subgraph Core["Core Logic"]
        BOT["bot.py<br/>Pipecat pipeline<br/>system prompt + tool routing"]
        FMT["formatter.py<br/>markdown to tmp/plain"]
        RAG["rag.py<br/>hybrid retrieval"]
    end

    subgraph External["External Services"]
        DEEPSEEK["DeepSeek API<br/>deepseek-chat"]
        DDG["DuckDuckGo<br/>via ddgs"]
    end

    subgraph Storage["Storage"]
        DOCSDIR["docs/ folder<br/>uploaded + crawled .md/.txt"]
        CHROMA[("chroma_db<br/>vectors + metadata")]
    end

    WEB --> CHAT
    UNITY --> CSHARP
    CSHARP --> CHAT
    WEB --> DOCS

    CHAT --> BOT
    CHAT --> FMT
    DOCS --> RAG
    BOT --> RAG
    BOT --> DEEPSEEK
    BOT --> DDG

    RAG --> DOCSDIR
    RAG --> CHROMA

    style BOT fill:#fef3c7
    style RAG fill:#dbeafe
    style DEEPSEEK fill:#fce7f3
```

---

## 4. Luồng xử lý 1 câu hỏi (Request Flow)

### 4.1 Sequence diagram

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant API as FastAPI /chat
    participant BOT as bot.py Pipecat
    participant LLM as DeepSeek API
    participant TOOL as Tool Handler
    participant RAG as rag.py
    participant DDG as DuckDuckGo

    U->>API: POST /chat (message, history, format)
    API->>BOT: run_chatbot(message, history)

    Note over BOT: Build system prompt<br/>(catalog tòa nhà từ docs/buildings)

    BOT->>LLM: chat.completions với tools schema
    LLM-->>BOT: tool_call search_info hoặc trigger_navigation

    alt User hỏi info (search_info)
        BOT->>TOOL: handle_search_info(query)
        TOOL->>RAG: query_vectorstore k=8
        Note over RAG: Hybrid BM25 + Semantic + RRF
        RAG-->>TOOL: top-8 chunks
        TOOL->>TOOL: _is_rag_useful >= 50% keywords?

        alt RAG useful
            TOOL-->>BOT: source=rag hits
        else RAG miss
            TOOL->>DDG: ddgs.text(query, max=2)
            DDG-->>TOOL: top-2 snippets
            TOOL-->>BOT: source=internet hits
        end

        BOT->>LLM: synthesize answer với tool result
        LLM-->>BOT: text response markdown

    else User yêu cầu chỉ đường (trigger_navigation)
        BOT->>TOOL: handle_trigger_navigation(destination)
        TOOL->>TOOL: _looks_like_building check guardrail
        alt destination valid
            TOOL-->>BOT: NavigationResult event + EndFrame
        else invalid
            TOOL-->>LLM: error - LLM retry với text fallback
        end
    end

    BOT-->>API: result type + content
    API->>API: convert_format md/tmp/plain
    API-->>U: JSON type + content
```

### 4.2 Diễn giải từng bước

1. **Client gửi POST /chat** với body `{message, history}` và query param `?format=md|tmp|plain`
2. **FastAPI** gọi `run_chatbot()` trong `bot.py`
3. **bot.py** build system prompt động:
   - Đọc `docs/buildings/*.md` lấy catalog (tên tòa + khoa) đưa vào prompt
   - System prompt mô tả 3 luồng + danh sách tools available
4. **Pipecat pipeline** chạy: user_aggregator → LLM → assistant_aggregator → output_collector
5. **DeepSeek** nhận message + tool schemas → quyết định gọi tool nào:
   - `trigger_navigation(destination_building)` — nếu user yêu cầu dẫn đường
   - `search_info(query)` — cho mọi câu hỏi thông tin
6. **Tool handler** thực thi tool, trả kết quả về LLM context
7. **DeepSeek round 2** đọc tool result, tổng hợp câu trả lời text
8. **OutputCollector** nhận text response, queue `EndFrame` để kết thúc pipeline
9. **api.py** áp dụng `format` conversion lên `content`:
   - `md`: giữ nguyên markdown
   - `tmp`: convert `**bold**` → `<b>bold</b>`, etc. cho Unity
   - `plain`: strip mọi markdown
10. **Response** JSON `{type: "text"|"navigation", content: ...}`

### 4.3 Timeout & error handling

```mermaid
flowchart LR
    START["Receive /chat"] --> TIMER["Start 30s timer<br/>asyncio.wait_for"]
    TIMER --> PIPELINE["Pipecat pipeline"]
    PIPELINE -->|"< 30s"| RESULT["Return result"]
    PIPELINE -->|">= 30s"| CANCEL["Cancel task"]
    CANCEL --> FALLBACK["Return text:<br/>Mình chưa phản hồi kịp trong 30s"]
```

### 4.4 Pipecat Pipeline — cấu trúc nội bộ

`run_chatbot()` ([bot.py:538](bot.py)) dựng một **Pipecat pipeline** mỗi request. Pipecat
là framework điều phối dialog theo mô hình **frame** chạy qua chuỗi **FrameProcessor**
nối tiếp. Mỗi xử lý là 1 processor; data (text, context, tool call) bọc trong các `Frame`
được đẩy tuần tự qua pipeline.

```mermaid
flowchart LR
    subgraph PIPE["Pipeline([...]) — chuỗi 4 processor"]
        direction LR
        UA["user_agg<br/>LLMContext user side<br/>gom user message"]
        LLM["OpenAILLMService<br/>gọi DeepSeek<br/>tool calling + stream"]
        AA["assistant_agg<br/>LLMContext assistant side<br/>gom response vào context"]
        OC["OutputCollector<br/>custom FrameProcessor<br/>bắt EndFrame + result"]
        UA --> LLM --> AA --> OC
    end

    PUSH["_push_message()<br/>add user msg<br/>queue LLMContextFrame"] --> UA
    LLM -.->|"tool_call frame"| TOOL["register_function handler<br/>trigger_navigation / search_info"]
    TOOL -.->|"result_callback"| LLM
    OC -->|"queue_frame"| END["EndFrame<br/>kết thúc task"]

    style LLM fill:#fef3c7
    style OC fill:#dbeafe
    style TOOL fill:#fce7f3
```

**Các thành phần dựng trong `run_chatbot()`**:

| Thành phần | Vai trò |
|---|---|
| `LLMContext(messages, tools)` | Source of truth — chứa system prompt + history + tools schema. Mọi processor đọc/ghi vào đây |
| `LLMContextAggregatorPair(context)` | Tách thành `user_agg` (gom message user) + `assistant_agg` (gom response LLM vào context) |
| `OpenAILLMService` | Gọi DeepSeek qua OpenAI-compatible API, stream text + emit tool call frame |
| `OutputCollector` | Processor cuối — phát hiện response xong → queue `EndFrame`, expose `.result` |
| `PipelineTask` + `PipelineRunner` | Chạy pipeline; `runner.run(task)` là coroutine chính được `asyncio.wait_for` bọc timeout |

**Vòng đời 1 request** (frame flow):

1. `_push_message()` add user message vào context → queue 1 `LLMContextFrame` vào đầu pipeline
2. `user_agg` nhận, đẩy context tới `OpenAILLMService`
3. **LLM round 1**: DeepSeek đọc context + tools → quyết định
   - Nếu **gọi tool** → emit tool call → Pipecat dispatch tới handler đã `register_function`
   - Handler chạy (RAG/internet/nav) → `params.result_callback(result, properties)` đẩy kết quả về context
   - `FunctionCallResultProperties(run_llm=True/False)` quyết định có chạy LLM round 2 không
4. **LLM round 2** (nếu `run_llm=True`): DeepSeek đọc tool result → sinh text trả lời
5. `assistant_agg` gom text/tool_calls vào context messages
6. `OutputCollector` bắt `LLMContextAssistantTimestampFrame`:
   - Nếu message cuối là **tool call chưa có text** → CHỜ (không kết thúc sớm)
   - Nếu là **text response hoàn chỉnh** → `task.queue_frame(EndFrame())` kết thúc
7. `runner.run(task)` return → đọc `collector.result` → `{type, content}`

**Vì sao cần `OutputCollector` tự quyết EndFrame?**
Pipeline không tự biết khi nào "xong" vì 1 turn có thể là 1 round (text thẳng) hoặc 2
round (tool call → text). Nếu gửi `EndFrame` ngay sau round 1, pipeline tắt khi LLM vừa
gọi tool mà chưa kịp đọc kết quả. Logic ở [bot.py:258-272](bot.py) check "message cuối
có phải tool call không có text không" để tránh tắt sớm.

**Vì sao mỗi request dựng pipeline mới?**
Stateless — history truyền qua param `conversation_history`, không giữ state giữa các
request. Đơn giản, tránh race condition khi nhiều client gọi đồng thời. Trade-off: overhead
dựng object nhỏ (~ms), không đáng kể so với LLM round-trip (~1-3s).

---

## 5. 3 Luồng quyết định (Decision Flow)

LLM quyết định gọi tool nào dựa trên user message:

```mermaid
flowchart TD
    Q["User Message"] --> CLASSIFY{"User message có pattern<br/>yêu cầu dẫn đường?<br/>chỉ đường / đi đến / dẫn tôi"}

    CLASSIFY -->|"Có"| NAV_RESOLVE{"Destination là<br/>TÊN TÒA hay khoa/phòng?"}
    NAV_RESOLVE -->|"Tòa"| NAV["trigger_navigation<br/>destination_building"]
    NAV_RESOLVE -->|"Khoa/Phòng"| NAV_LOOKUP{"Có trong DANH BẠ<br/>system prompt?"}
    NAV_LOOKUP -->|"Có"| NAV
    NAV_LOOKUP -->|"Không"| NAV_SEARCH["search_info tìm khoa to tòa"]
    NAV_SEARCH --> NAV_OK{"Tìm được tòa cụ thể?"}
    NAV_OK -->|"Có"| NAV
    NAV_OK -->|"Không"| TEXT_NF["Text: Mình không biết<br/>khoa này ở tòa nào"]

    CLASSIFY -->|"Không"| INFO["search_info<br/>query=truy vấn"]
    INFO --> RAG_CHECK{"RAG hits useful?<br/>>= 50% keyword match"}
    RAG_CHECK -->|"Có"| TEXT_RAG["Text trả lời<br/>từ RAG context"]
    RAG_CHECK -->|"Không"| INTERNET["DDG search<br/>top-2 snippet"]
    INTERNET --> NET_OK{"Có hits?"}
    NET_OK -->|"Có"| TEXT_NET["Text trả lời từ internet<br/>cite URL"]
    NET_OK -->|"Không"| TEXT_NO["Text:<br/>Chưa tìm được thông tin"]

    NAV --> GUARD{"Destination looks<br/>like building?"}
    GUARD -->|"Có"| NAV_OUT["Output type=navigation<br/>destination_building"]
    GUARD -->|"Không<br/>khoa/phòng/dot"| NAV_REJECT["Reject + tell LLM<br/>fallback text"]
    NAV_REJECT --> TEXT_NF

    style NAV fill:#fef3c7
    style INFO fill:#dbeafe
    style TEXT_NO fill:#fee2e2
    style TEXT_NF fill:#fee2e2
```

### Tại sao chia 3 luồng?

- **Luồng 1 (Navigation)**: kích hoạt event điều hướng — output structured data, không phải text
- **Luồng 2 (Info, RAG → Internet)**: trả lời text. **Code orchestrate fallback** (không phải LLM tự quyết) → đảm bảo nhất quán, LLM không "lười" stop ở RAG khi RAG không có data
- **Luồng 3 (Not found)**: chỉ kích hoạt khi cả RAG lẫn Internet đều fail → tránh bịa thông tin

---

## 6. RAG Pipeline (Indexing)

### 6.1 Build vector store

```mermaid
flowchart LR
    A["docs/*.md<br/>docs/*.txt"] --> B["_load_docs<br/>parse frontmatter<br/>mark is_uploaded"]
    B --> C["Document objects<br/>page_content + metadata"]
    C --> D["RecursiveCharacterTextSplitter<br/>chunk=500 overlap=50"]
    D --> E["Chunks ~150 đoạn"]
    E --> F["JinaEmbeddings<br/>jina-embeddings-v3 API<br/>batch 32/request"]
    F --> G["Vector 1024D mỗi chunk"]
    G --> H[("Chroma persistent<br/>chroma_db/")]

    style F fill:#fce7f3
    style H fill:#dbeafe
```

**Chi tiết các bước**:

1. **Load** (`rag.py:_load_docs`):
   - Scan đệ quy `docs/`, lấy mọi `.md` và `.txt`
   - Parse YAML frontmatter (chỉ `.md`)
   - Mark `is_uploaded=true` cho file trong `docs/uploaded/` (để boost sau)

2. **Chunk** (`rag.py:build_vectorstore`):
   - `RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50, separators=["\n\n", "\n", ". ", " ", ""])`
   - Cố split tại boundary tự nhiên (paragraph > newline > câu > từ)
   - Overlap 50 chars để câu trả lời vắt qua biên không bị cắt

3. **Embed**:
   - Model: `jina-embeddings-v3` — multilingual bi-encoder của Jina AI, MTEB top tier
   - **Gọi REST API** `https://api.jina.ai/v1/embeddings`, batch 32 chunks/request
   - Mỗi chunk → 1024-dim float vector
   - Task mode: `retrieval.passage` cho docs, `retrieval.query` cho query (Jina v3 tách 2 mode để optimize asymmetric retrieval)
   - **0 RAM local**, không phụ thuộc torch/CUDA → fit Render free tier (512MB)

4. **Store**:
   - Chroma persistent → SQLite + HNSW (Hierarchical Navigable Small World) index
   - `chroma_db/chroma.sqlite3` + binary files
   - Survive restart, không cần rebuild

### 6.2 Hybrid Retrieval (query time)

```mermaid
flowchart TB
    Q["Query: Khoa Cơ khí ở tòa nào"] --> SEM["Semantic Search<br/>Chroma similarity<br/>top-2k chunks"]
    Q --> KEY["BM25 keyword<br/>top-2k chunks"]

    SEM --> RRF["Reciprocal Rank Fusion<br/>score = sum weight/(60+rank)"]
    KEY --> RRF

    RRF --> WEIGHTS{"Apply weights"}
    WEIGHTS -->|"is_uploaded=true"| BOOST["x 2.5"]
    WEIGHTS -->|">40% là links/URLs"| PENALTY["x 0.4"]
    WEIGHTS -->|"else"| NORMAL["x 1.0"]

    BOOST --> MERGE["Merge + dedup<br/>by content prefix"]
    PENALTY --> MERGE
    NORMAL --> MERGE

    MERGE --> RERANK["_rerank_for_location<br/>nếu query hỏi vị trí<br/>boost chunks Tòa X là của Y"]
    RERANK --> TOPK["Top-K final chunks"]
    TOPK --> LLM["LLM context"]
```

**Vì sao cần Hybrid?**

- **Semantic only** (chỉ embeddings): yếu cho keyword match tiếng Việt ngắn (vd "Khoa Cơ khí"). Match nhầm với "Xưởng cơ khí C1" trong noise text.
- **BM25 only**: không hiểu ngữ nghĩa, fail với câu phrasing khác từ doc.
- **Kết hợp 2 + RRF**: ổn định với cả 2 case — đây là approach của LangChain `EnsembleRetriever`, NVIDIA, Anthropic, etc.

**Boost & Penalty (cải tiến riêng cho project này)**:
- **×2.5 cho uploaded**: file user upload có content curated, signal mạnh hơn crawled
- **×0.4 cho link-heavy chunks**: `hcmut.edu.vn` SPA crawl ra nhiều chunks chỉ là `[link](drive.google.com/...)`. Penalty này giảm noise

**Re-rank** (`bot.py:_rerank_for_location`):
- Khi query có pattern "ở tòa nào", boost chunks chứa pattern `Tòa X là của Y`
- Fix bug LLM nhầm "Xưởng cơ khí C1" với "Khoa Cơ khí ở C1"

---

## 7. Document Upload Pipeline

```mermaid
sequenceDiagram
    actor U as User
    participant FE as Frontend DocsPanel
    participant API as /docs/upload
    participant FS as Filesystem
    participant BG as Background Task
    participant RAG as build_vectorstore

    U->>FE: Drop file (.md/.txt/.pdf) hoặc paste URL
    FE->>API: POST multipart hoặc JSON url

    alt PDF
        API->>API: pypdf extract text
        API->>FS: Save as .txt
    else TXT
        API->>FS: Save .txt nguyên content
    else MD
        API->>FS: Save .md + frontmatter
    else URL crawl
        API->>API: requests + BS4 + html2text
        API->>FS: Save .md
    end

    API->>BG: schedule _rebuild_index_safe
    API-->>FE: 200 OK filename + rebuild_scheduled

    BG->>RAG: build_vectorstore()
    Note over RAG: Load all docs<br/>chunk + embed + index
    RAG-->>BG: n chunks indexed
    BG->>BG: update _rebuild_state.last_at

    loop poll every 3s
        FE->>API: GET /docs/rebuild/status
        API-->>FE: running, last_at, last_chunks
    end
```

---

## 8. Multi-format Output (Web vs Unity)

Cùng 1 markdown response, 3 format output khác nhau:

```mermaid
flowchart LR
    LLM["DeepSeek text<br/>markdown"] --> CONVERT{"format param"}

    CONVERT -->|"md default"| MD["bold + bullet + link<br/>markdown raw"]
    CONVERT -->|"tmp"| TMP["b tags + bullet + link tags<br/>TMP rich text"]
    CONVERT -->|"plain"| PLAIN["bold bullet link<br/>plain stripped"]

    MD --> WEB["Web Dashboard<br/>marked.js + DOMPurify"]
    TMP --> UNITY["Unity TextMeshPro<br/>tmpText.text = response"]
    PLAIN --> OTHER["Plain text client"]

    style TMP fill:#fef3c7
    style MD fill:#dbeafe
```

**Conversion regex** (`formatter.py`):

| Markdown | TMP rich text |
|---|---|
| `**bold**` | `<b>bold</b>` |
| `*italic*` | `<i>italic</i>` |
| `# H1` | `<size=140%><b>H1</b></size>` |
| `[text](url)` | `<link="url"><color=#2563eb><u>text</u></color></link>` |
| `- bullet` | `• bullet` |
| `> quote` | `<color=#666666>│ quote</color>` |

---

## 9. Tổng kết Components & Files

| File | Vai trò | Lines |
|---|---|---|
| [api.py](api.py) | FastAPI server, `/chat` endpoint, mount docs router, serve frontend | ~160 |
| [bot.py](bot.py) | Pipecat pipeline, system prompt, 2 tool handlers, run_chatbot() | ~570 |
| [rag.py](rag.py) | Hybrid retrieval (BM25 + semantic + RRF), build/query vectorstore | ~250 |
| [routers/docs.py](routers/docs.py) | Knowledge base CRUD endpoints (upload, crawl URL, rebuild) | ~280 |
| [formatter.py](formatter.py) | Markdown → TMP rich text / plain converter | ~120 |
| [auth.py](auth.py) | API key header verification | ~20 |
| [scripts/crawl.py](scripts/crawl.py) | Playwright crawler cho SPA sites | ~160 |
| [scripts/build_index.py](scripts/build_index.py) | Script CLI gọi `build_vectorstore()` | ~15 |
| [frontend/](frontend/) | Vue 3 dashboard (Chat + Knowledge Base tabs) | — |

---

## 10. Quyết định thiết kế quan trọng

| Vấn đề | Giải pháp | Lý do |
|---|---|---|
| Bot bịa tên tòa khi không biết | Code guardrail `_looks_like_building()` reject "Khoa X", "phòng F1.05" | Prompt-only không đủ — LLM eager. Code-level enforce |
| Bot ưu tiên RAG generic chunks | Boost uploaded ×2.5 + Link penalty ×0.4 | User-uploaded curated > crawled noise (Drive links spam) |
| Render free tier 512MB RAM không fit local HF model | Move embedding → Jina v3 API | Free 1M token/tháng, 0 RAM local, MTEB top, không cần torch/CUDA |
| LLM "lười" không fallback internet | Gộp `search_knowledge` + `search_internet` → 1 `search_info`, code orchestrate fallback | Bypass LLM decision, đảm bảo nhất quán |
| hcmut.edu.vn là SPA | Dùng Playwright thay vì requests | Body shell rỗng với requests, JS render mới có content |
| Cold start nhanh hơn local model | Jina API call ~200ms vs HF model load ~10s | Embed API call có overhead network nhưng không cần load model |
| Hard timeout request | `asyncio.wait_for(30s)` + cancel task + fallback text | Tránh user đợi vô tận khi LLM/network hang |
| Multi-client (web/Unity) | `?format=md/tmp/plain` query param, code convert ở api layer | Source of truth = markdown từ LLM, client chỉ định format mong muốn |

---

## 11. Persistence trên Render

Câu hỏi quan trọng cho deploy: **vector database được lưu thế nào?**

### Free tier (mặc định)

- **KHÔNG có persistent disk** — mỗi lần deploy/restart, filesystem reset hoàn toàn
- Hệ quả:
  - `chroma_db/` (vector index): **bị xóa** → auto rebuild từ `docs/` khi server startup (~10-30s)
  - `docs/uploaded/` (user upload): **bị xóa** nếu không commit vào git
  - `docs/*.md` từ crawl/seed: **giữ** nếu commit vào git
- Cơ chế tự rebuild (`api.py:_warmup_rag`):

```python
@app.on_event("startup")
async def _warmup_rag():
    if not has_chroma and has_docs:
        build_vectorstore()   # rebuild từ docs/ (qua Jina API)
    query_vectorstore("warmup", 1)  # smoke test Jina endpoint
```

- **Embedding qua Jina API** (không cần load model local) → cold start nhanh, free tier 512MB RAM vừa đủ
- Index rebuild 156 chunks: ~5 batch × 32 chunks/batch → 5-7s qua Jina API
- Cost: ~25K token cho 156 chunks → free tier 1M/tháng cover thoải mái

### Starter plan ($7/month)

Bật persistent disk trong `render.yaml` (đã có sẵn block comment):

```yaml
disk:
  name: data
  mountPath: /opt/render/project/src/chroma_db
  sizeGB: 1
```

→ `chroma_db/` persist qua deploys. Có thể thêm disk thứ 2 cho `docs/uploaded/`.

### Khuyến nghị thực tế

| Use case | Persistence strategy |
|---|---|
| Demo / dev | Free tier OK. Auto-rebuild thêm ~30s cold start nhưng acceptable |
| Production small | Free tier + commit `docs/*` vào git để giữ knowledge base qua redeploy |
| Production có user upload | Starter plan với persistent disk |
| Production heavy | Move chroma sang managed (Pinecone, Qdrant Cloud, Weaviate Cloud) — out of scope |

---

## 12. Trade-offs & Hạn chế

| Hạn chế | Workaround / Note |
|---|---|
| Render free tier không persist `docs/uploaded/` + `chroma_db/` | Commit vào git, hoặc upgrade Starter plan |
| Mỗi query cần 1 round-trip Jina API (~200ms) | Acceptable — tradeoff để fit free tier RAM. Có thể cache embedding nếu cần |
| Jina free tier 1M token/tháng | Đủ cho ~5K-10K query/tháng. Nếu vượt → upgrade Jina paid ($0.018/1M) hoặc switch provider |
| PDF extract format kém với tables | `pypdf` cơ bản. PDF tables phức tạp → khuyến nghị convert markdown thủ công trước khi upload |
| LLM stochastic — đôi khi inconsistent | Bump retry, hoặc dùng `temperature=0` (hiện 0.3) |
| Tiếng Việt RAG chưa hoàn hảo cho mọi case | Combine BM25 + boost + re-rank. Hybrid retrieval gần với SOTA |
| DDG rate limit / chậm | Switch sang Tavily nếu cần (set `TAVILY_API_KEY`) |
| Pipeline timeout 30s | Tăng `CHATBOT_TIMEOUT_SECS` nếu chained 2 tool calls quá lâu |

---

## 13. Để thuyết trình — Tóm tắt 5 phút

### Slide 1: Vấn đề
"Sinh viên HCMUT khó tra cứu thông tin trường, không biết khoa nào ở tòa nào, không biết hỏi ai về học phí. Cần chatbot tích hợp được vào app Unity 3D bản đồ trường."

### Slide 2: Kiến trúc
- 3 layer: Client → FastAPI → LLM/RAG/Internet
- Chatbot có 2 tool: `trigger_navigation` + `search_info`
- 1 endpoint `/chat?format=tmp` cho Unity

### Slide 3: RAG
- Knowledge base: upload `.md/.txt/.pdf` qua web UI
- Index: chunk 500 chars → embed Jina v3 API (multilingual, 1024D) → Chroma
- Retrieve: hybrid BM25 + semantic, RRF merge, boost user files
- Quality: 6/7 query đúng building, 100% query curriculum trả lời đúng số tín chỉ
- Architecture choice: embedding qua API (Jina free tier) thay vì load local — fit Render 512MB RAM

### Slide 4: 3 Luồng
- Luồng 1: Navigation (có guardrail anti-hallucination)
- Luồng 2: Info — RAG trước, code tự fallback Internet (DuckDuckGo)
- Luồng 3: "Không tìm thấy" — chỉ khi cả 2 fail

### Slide 5: Unity Integration
- DeepSeek output markdown
- Backend convert sang TMP rich text (`<b>`, `<color>`, `<link>`)
- Unity TextMeshPro render trực tiếp

### Slide 6: Demo flow

Live demo 4-5 query:

1. "Khoa Cơ khí ở tòa nào" → Tòa B11 (RAG hit, ~2s)
2. "Chỉ đường tới Khoa Cơ khí" → trigger_navigation Tòa B11 (catalog resolve)
3. "Hiệu trưởng HCMUT là ai" → search_info auto fallback internet (~4s)
4. "Tổng tín chỉ KHMT" → 128 (curriculum file user upload)
5. "Phòng F1.05" → bot từ chối "không biết tòa nào" (anti-hallucination)

---

## 14. Tham khảo nhanh

- **API docs**: `http://localhost:8000/swagger` (FastAPI auto)
- **Setup**: [CLAUDE.md](CLAUDE.md)
- **Deploy**: [DEPLOY.md](DEPLOY.md)
- **Code**: [bot.py](bot.py) (core), [rag.py](rag.py) (retrieval)
