# neuroflash Gateway

OpenAI-совместимый шлюз для [neuroflash AI](https://neuroflash.com) — 9+ моделей, включая Claude Opus 5.5, GPT-6-luna, GPT-6-sol, Claude Sonnet 5.5, Gemini 3.8 Flash, с контекстом до **1M токенов**.

```bash
export NF_CLIENT_ID="your-client-id"
export NF_CLIENT_SECRET="your-client-secret"
python3 nf_gate.py
# → OpenAI-совместимый API на :8088
```

## ✨ Возможности

| | |
|---|---|
| **OpenAI API** | `GET /v1/models`, `POST /v1/chat/completions` |
| **Streaming** | SSE, `data: [DONE]`, `Connection: close` |
| **Tool calling** | **Нативный passthrough** — Claude/GPT/Gemini получают `tools` напрямую |
| **Workspace** | **Авто-обнаружение** — не нужно указывать `NF_WORKSPACE_ID` |
| **Auth** | OAuth2 Client Credentials (токен авто-refresh, 401 → retry) |
| **Aктуальные модели** | `GET /v1/models` — живые, с `available` флагом (не хардкод) |
| **Зависимости** | **0** — только Python 3.7+ (stdlib: `http.server`, `urllib`, `json`) |
| **OMP / MCP / Any-OpenAI** | Совместим на уровне протокола |

## 🚀 Быстрый старт

1. Зарегистрируйтесь на [app.neuroflash.com/register](https://app.neuroflash.com/register) (карта не нужна)
2. **Settings → Workspace → API Access → Create Service Account**
3. Скопируйте `Client ID` и `Client Secret`
4. Запустите шлюз:

```bash
export NF_CLIENT_ID="ваш-client-id"
export NF_CLIENT_SECRET="ваш-client-secret"
python3 nf_gate.py
```

Workspace ID определится автоматически (первый workspace аккаунта).

**Результат:**
```
auth: OK
workspace: auto a255b182-...
models: 9 available of 27
  - claude-opus-5.5
  - claude-sonnet-5.5
  - gpt-6-luna
  ...
neuroflash gateway on :8088
```

## 🔌 Примеры

```bash
# список моделей (только доступные)
curl http://localhost:8088/v1/models

# все модели (включая недоступные)
curl 'http://localhost:8088/v1/models?all=1'

# chat completion (streaming)
curl -X POST http://localhost:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-5.5","messages":[{"role":"user","content":"Привет!"}],"stream":true}'

# с tool calling (native passthrough)
curl -X POST http://localhost:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"claude-sonnet-5.5",
    "messages":[{"role":"user","content":"Погода в Париже?"}],
    "tools":[{"type":"function","function":{"name":"get_weather","description":"Погода","parameters":{"type":"object","properties":{"city":{"type":"string","description":"Город"}},"required":["city"]}}}]
  }'

# health
curl http://localhost:8088/health
```

## 🔧 Переменные окружения

| Переменная | Обязательно | Описание |
|---|---|---|
| `NF_CLIENT_ID` | ✅ | Service Account ID |
| `NF_CLIENT_SECRET` | ✅ | Service Account Secret |
| `NF_WORKSPACE_ID` | нет | Переопределить workspace (если не auto) |
| `NF_DEFAULT_MODEL` | нет | Модель по умолчанию (`claude-sonnet-5.5`) |
| `NF_MODEL_MAP` | нет | Алиасы: `local-name:upstream-name,cheap:gpt-4.1-mini` |

## 🖥 OMP Integration

```bash
omp config set provider nf base_url=http://localhost:8088
omp config set provider nf api_key=any
omp config set provider nf models claude-sonnet-5.5,gpt-6-luna,gpt-6-sol
```

## 🔐 Security

- Секреты — только через `.env` (файл в `.gitignore`)
- **Смените secret** после публикации репо: Settings → API Access → New Service Account → удалить старый
- В коммитах нет реальных credentials (проверено `git grep`)

## 📦 Зависимости

**Нулевые.** Только Python stdlib.

## 📋 Модели (пример — фактический список через `/v1/models`)

Количество доступных моделей зависит от плана. `GET /v1/models` возвращает `available: true/false` для каждой:

| Модель | Провайдер | Контекст |
|---|---|---|
| `claude-opus-5.5` | Anthropic | 1M |
| `claude-sonnet-5.5` | Anthropic | 1M |
| `gpt-6-luna` | OpenAI | 1.05M |
| `gpt-6-sol` | OpenAI | 1.05M |
| `gemini-3.8-flash` | Google | 1.05M |
| `gpt-4.1-mini` | OpenAI | 128K |
| `claude-haiku-4.5` | Anthropic | 200K |
| `mistral-medium-3.1` | Mistral | 131K |
| `gemini-3.1-pro-preview` | Google | 1.05M |
| … | … | … |

## ✅ Live-тесты

Всё проверено (eval, gateway live, streaming, tools):

```
HEALTH: 200  {"ok":true,"workspace":"a255b182-..."}
MODELS: 27 total, 9 available
CHAT:  finish_reason="stop"
TOOLS: finish_reason="tool_calls" — native passthrough (get_weather, Paris)
STREAM: [DONE] received, content OK
```