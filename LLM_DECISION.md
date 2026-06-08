# Cách LLM đưa ra quyết định

## 1. LLM là gì về mặt kỹ thuật?

LLM về bản chất là một hàm toán học:

```
f(token_1, token_2, ..., token_n) → token_tiếp_theo
```

Nó không "suy nghĩ" theo nghĩa con người. Nó **dự đoán token tiếp theo có xác suất cao nhất** dựa trên toàn bộ chuỗi token đầu vào. Mọi quyết định — gọi tool hay trả lời text — đều là kết quả của quá trình dự đoán này.

---

## 2. System prompt ảnh hưởng thế nào?

Khi bạn đưa system prompt vào, LLM không "đọc hiểu" theo nghĩa con người. Trong quá trình **fine-tuning**, nhà cung cấp (Groq, OpenAI...) đã huấn luyện mô hình để:

- Nội dung ở vị trí `role: system` có **trọng số ảnh hưởng cao hơn** các role khác
- LLM học pattern: "khi system nói X, hãy làm Y"

Ví dụ thực tế trong dự án — toàn bộ messages gửi lên Groq:

```json
[
  {
    "role": "system",
    "content": "Bạn là trợ lý HCMUT...\n\nNHÓM A — hỏi thông tin → gọi get_building_info\nNHÓM B — chỉ đường → gọi trigger_navigation\n\nCÁC ĐỊA ĐIỂM:\n- key=`A4` → Tòa nhà A4\n- key=`thu-vien` → Thư viện Trung tâm\n..."
  },
  {
    "role": "user",
    "content": "Xin chào"
  },
  {
    "role": "assistant",
    "content": "Xin chào! Tôi có thể giúp gì cho bạn?"
  },
  {
    "role": "user",
    "content": "Thư viện có bao nhiêu đầu sách?"   ← tin nhắn mới nhất
  }
]
```

LLM đọc toàn bộ chuỗi này → dự đoán token tiếp theo.
Vì system đã dặn "hỏi thông tin → gọi get_building_info", xác suất output là `tool_call` cao hơn nhiều so với text thông thường.

---

## 3. Tool calling — LLM quyết định gọi tool thế nào?

Đây là phần quan trọng nhất. Khi gửi request lên Groq, ngoài `messages`, ta còn gửi `tools`:

```json
{
  "model": "llama-3.3-70b-versatile",
  "messages": [...],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_building_info",
        "description": "Tra cứu thông tin chi tiết của một tòa nhà theo key. Gọi khi người dùng hỏi về mô tả, dịch vụ, số tầng...",
        "parameters": {
          "type": "object",
          "properties": {
            "key": { "type": "string", "description": "Key tòa nhà, ví dụ: 'A4', 'thu-vien'" }
          },
          "required": ["key"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "trigger_navigation",
        "description": "Gọi KHI VÀ CHỈ KHI người dùng muốn chỉ đường. KHÔNG gọi cho câu hỏi thông tin.",
        "parameters": { ... }
      }
    }
  ]
}
```

LLM trả về **một trong hai dạng**:

**Dạng 1 — Text bình thường** (`finish_reason: "stop"`):
```json
{
  "choices": [{
    "message": { "role": "assistant", "content": "Xin chào! Tôi có thể giúp gì?" },
    "finish_reason": "stop"
  }]
}
```

**Dạng 2 — Tool call** (`finish_reason: "tool_calls"`):
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_building_info",
          "arguments": "{\"key\": \"thu-vien\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**Tại sao LLM biết chọn tool nào?** → Dựa vào `description` của từng tool. Description càng rõ ràng, phân biệt càng sắc nét → LLM chọn đúng hơn. Đây là lý do `description` trong code dùng chữ "KHI VÀ CHỈ KHI" và ghi rõ "KHÔNG gọi cho câu hỏi thông tin".

---

## 4. Vị trí trong code

### System prompt — `bot.py` dòng 87–116

```python
_SYSTEM_PROMPT_TEMPLATE = """\
Bạn là trợ lý ảo thông minh của ĐHBK TP.HCM...

### NHÓM A — Câu hỏi thông tin → GỌI get_building_info trước
Khi người dùng hỏi chi tiết về một địa điểm:
1. Gọi get_building_info với key tương ứng
2. Dùng dữ liệu trả về để trả lời

### NHÓM B — Muốn đi đến địa điểm → GỌI trigger_navigation
Từ khóa: "chỉ đường", "đi đến", "dẫn tôi đến"...
"""
```

→ Đây là nơi bạn **lập trình hành vi** của LLM bằng ngôn ngữ tự nhiên.

---

### Tool description — `bot.py` dòng 140–174

```python
navigation_tool = FunctionSchema(
    name="trigger_navigation",
    description=(
        "Gọi hàm này KHI VÀ CHỈ KHI người dùng muốn được chỉ đường..."
        "Không gọi hàm này cho các câu hỏi thông tin thông thường."
    ),
)

building_info_tool = FunctionSchema(
    name="get_building_info",
    description=(
        "Tra cứu thông tin chi tiết của một tòa nhà theo key. "
        "Gọi khi người dùng hỏi về mô tả, dịch vụ, số tầng, khoa..."
    ),
)
```

→ **Yếu tố quyết định quan trọng nhất** cho tool calling. LLM đọc description để phân biệt 2 tool.

---

### Lắp ráp context — `bot.py` dòng 365–370

```python
system_prompt = await _build_system_prompt()   # system + mục lục tòa nhà từ DB
messages = [
    {"role": "system", "content": system_prompt},
    *conversation_history,                      # ← lịch sử hội thoại trước
]
context = LLMContext(messages=messages, tools=tools_schema)
```

→ Đây là nơi ghép tất cả lại: system + history + message mới + tools, rồi gửi lên LLM.

---

## 5. Toàn bộ chuỗi quyết định

```
User: "Thư viện có bao nhiêu sách?"
        ↓
[system prompt] + [history] + [message mới] + [tools]
gửi lên Groq
        ↓
LLM so khớp pattern:
  - "hỏi về tòa nhà cụ thể" ← từ tin nhắn
  - "gọi get_building_info khi hỏi thông tin" ← từ system prompt
  - description của get_building_info khớp ← từ tool schema
  → xác suất cao nhất → output tool_call
        ↓
Pipecat nhận tool_call { name: "get_building_info", arguments: {"key": "thu-vien"} }
        ↓
handle_get_building_info() → db_get_building("thu-vien") → MongoDB
        ↓
tool_result: { ten, mo_ta, tang, dich_vu, ... } → đưa vào context
        ↓
LLM chạy lại lần 2 với data mới
        ↓
Output: "Thư viện Trung tâm ĐHBK có hơn 200,000 đầu sách..."
```

---

## 6. Điều quan trọng cần nhớ cho phản biện

**Bạn không kiểm soát 100% hành vi LLM** — bạn chỉ có thể **tăng xác suất** LLM làm đúng thông qua:

| Yếu tố | Tác động |
|---|---|
| System prompt rõ ràng | LLM hiểu vai trò và quy tắc |
| Tool description chính xác | LLM phân biệt được khi nào gọi tool nào |
| Ví dụ cụ thể trong prompt | LLM học theo pattern ví dụ (few-shot) |
| Nhiệt độ thấp (`temperature=0.3`) | Giảm sự ngẫu nhiên → ổn định hơn |
| Model mạnh (70B) | Hiểu ngữ cảnh tốt hơn, ít sai hơn |

LLM vẫn có thể sai (gọi nhầm tool, không gọi khi cần). Đó là lý do trong thực tế cần thêm validation, fallback, hoặc human-in-the-loop.
