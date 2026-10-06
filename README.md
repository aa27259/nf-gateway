# neuroflash Gateway

OpenAI-совместимый шлюз для [neuroflash AI](https://neuroflash.com) — **27 моделей, включая Claude Opus 5.5, GPT-6, Claude Sonnet 5.5, Gemini 3.8 Flash** с контекстом до **1M токенов**.

```bash
# один файл, никаких зависимостей (stdlib-only)
python3 nf_gate.py
# → OpenAI-совместимый API на :8088
```

## ✨ Возможности

| | Поддержка |
|---|---|
| **OpenAI API** | `GET /v1/models`, `POST /v1/chat/completions` |
| **Streaming** | SSE, `data: [DONE]` |
| **2 режима auth** | Service Account (OAuth2) или Session Cookie |
| **Tool/Function calling** | эмуляция через system prompt |
| **Модели** | Claude Opus 5.5, GPT-6, GPT-4.1, Gemini 3.8, Mistral… **27 моделей** |
| **Контекст** | до **1 050 000 токенов** |
| **Зависимости** | **0** — только Python 3.7+ |
| **Готов к MCP/OMP** | совместим с OpenAPI, Any-openai |

## 🚀 Быстрый старт

### Способ 1 — Service Account (OAuth2)

1. Зарегистрируйтесь на [app.neuroflash.com](https://app.neuroflash.com)
2. Зайдите в **Settings → Workspace → API Access** → Create Service Account
3. Скопируйте `client_id` и `client_secret`
4. Получите `workspace_id`:

```bash
curl -s https://id.neuroflash.com/oauth/v2/token \
  -d "grant_type=client_credentials&client_id=YOUR_ID&client_secret=YOUR_SECRET&scope=openid" \
  | python3 -c "import sys,json;tok = json.load(sys.stdin)['access_token']; print(tok)"
# сохраните токен, выполните:
curl -H "Authorization: Bearer TOKEN" \
  https://app.neuroflash.com/api/workspace-service/v1/workspaces
# → найдите "id" в ответе (UUID)
```

5. Запустите шлюз:

```bash
export NF_CLIENT_ID="your-client-id"
export NF_CLIENT_SECRET="your-client-secret"
export NF_WORKSPACE_ID="your-workspace-id"
python3 nf_gate.py
```

### Способ 2 — Session Cookie (JWT)

Используйте JWT из куки `nf-access-token` (если есть доступ к моделям, не через Service Account):

```bash
export NF_TOKEN="eyJhbGciOiJSUzI1NiIs..."
export NF_WORKSPACE_ID="your-workspace-id"
python3 nf_gate.py
```

## 🔌 Использование

```bash
# список моделей
curl http://localhost:8088/v1/models | python3 -m json.tool

# chat completion (streaming)
curl -X POST http://localhost:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-5.5",
    "messages": [{"role": "user", "content": "Hi!"}],
    "stream": true
  }'

# с tool calling
curl -X POST http://localhost:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-5.5",
    "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get weather for a city",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {"type": "string", "description": "City name"}
            },
            "required": ["city"]
          }
        }
      }
    ]
  }'
```

## 🔧 Переменные окружения

| Переменная | Обязательно | Описание |
|---|---|---|
| `NF_CLIENT_ID` | для OAuth2 | Service Account ID |
| `NF_CLIENT_SECRET` | для OAuth2 | Service Account Secret |
| `NF_TOKEN` | для Session | JWT из куки `nf-access-token` |
| `NF_WORKSPACE_ID` | да | ID вашего workspace |
| `NF_MODEL_MAP` | нет | алиасы моделей: `alias:real-name,alias2:real2` |

## 🖥 OMP / Any-OpenAI Integration

```bash
omp config set provider neuroflash base_url=http://localhost:8088
omp config set provider neuroflash api_key=any
```

## 🔐 Security

- **Секреты НЕ хардкодить** — используйте `.env` (в `.gitignore`)
- После публикации репо **смените secret** в Settings → Workspace → API Access
- Репозиторий проверен на отсутствие секретов

## 📦 Зависимости

**Нулевые.** Чистый Python stdlib: `http.server`, `urllib`, `json` — ничего ставить не надо.

## 📋 Модели (27 шт)

| Модель | Контекст | Status |
|---|---|---|
| `claude-opus-5.5` | 1 000 000 | ✅ |
| `claude-sonnet-5.5` | 1 000 000 | ✅ |
| `gpt-6-luna` | 1 050 000 | ✅ |
| `gpt-6-sol` | 1 050 000 | ✅ |
| `gemini-3.8-flash` | 1 048 576 | ✅ |
| `gpt-4.1-mini` | 128 000 | ✅ |
| +22 другие | — | ✅ |