Full API reference for this deployment. Two API layers exist: the Open-WebUI gateway (auth + OpenAI-compatible) and the raw Ollama engine.

## Endpoints

| Layer | Base URL | Auth |
| --- | --- | --- |
| Open-WebUI (public, HTTPS) | `https://ollamallm.tools.thefusionapps.com` | API key (Bearer) |
| Open-WebUI (host) | `http://localhost:8088` | API key (Bearer) |
| Open-WebUI (LAN) | `http://192.168.0.106:8088` | API key (Bearer) |
| Ollama native (host) | `http://172.17.0.1:11434` | none |
| Ollama native (LAN) | `http://192.168.0.106:11434` | none (only if bound to `0.0.0.0`) |
- Container port `8080` → host port `8088` → public domain via reverse proxy.
- Frontend and backend share the same port (single FastAPI process).

## Available Models

- `gemma3:12b`
- `qwen2.5vl:3b` (vision)
- `llama3.1:8b`
- `gemma4:e4b`

## Get an API Key

1. Open `https://ollamallm.tools.thefusionapps.com`
2. Settings → Account → API Keys → Create new key
3. Admin must enable it once: Settings → Admin Settings → General → "Enable API Key"

Set it as an env var to reuse below:

```bash
export OWUI_URL="<https://ollamallm.tools.thefusionapps.com>"
export OWUI_KEY="sk-8e99f20f2eb8204ec8c2eb9f7f243517192f2f962ad02a05"
```

> This key belongs to the admin user (`caveman-cli`, no expiry). Treat as a secret. Rotate/delete from Settings → Account → API Keys, or in the `api_key` DB table, if leaked.
> 

---

## Open-WebUI API (OpenAI-compatible, recommended)

### List models

```bash
curl "$OWUI_URL/api/models" \\
  -H "Authorization: Bearer $OWUI_KEY"
```

### Chat completion (OpenAI-compatible)

```bash
curl "$OWUI_URL/api/chat/completions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Explain RAG in one sentence."}
    ]
  }'
```

### Streaming

```bash
curl "$OWUI_URL/api/chat/completions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "llama3.1:8b",
    "messages": [{"role": "user", "content": "Count to 5"}],
    "stream": true
  }'
```

### Vision (qwen2.5vl:3b — image input)

```bash
curl "$OWUI_URL/api/chat/completions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "qwen2.5vl:3b",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<BASE64>"}}
      ]
    }]
  }'
```

### Ollama proxied through Open-WebUI (with auth)

```bash
# Native Ollama API, but authenticated via Open-WebUI
curl "$OWUI_URL/ollama/api/tags" \\
  -H "Authorization: Bearer $OWUI_KEY"

curl "$OWUI_URL/ollama/api/generate" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "llama3.1:8b", "prompt": "Hello", "stream": false}'
```

### RAG — upload file then chat over it

```bash
# 1. Upload file
curl "$OWUI_URL/api/v1/files/" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -F "file=@./document.pdf"
# returns {"id": "<file_id>", ...}

# 2. Chat referencing the file
curl "$OWUI_URL/api/chat/completions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "llama3.1:8b",
    "messages": [{"role": "user", "content": "Summarize the document."}],
    "files": [{"type": "file", "id": "<file_id>"}]
  }'
```

---

## Input Types — Images, Files, Audio

The API accepts three kinds of non-text input. Each works differently.

### 1. Images (vision) — inline base64 or URL

Send images directly inside the message. The `content` field becomes an **array** mixing `text` and `image_url` items. Requires a vision-capable model (`qwen2.5vl:3b`). No embedding/RAG — the image goes straight to the model.

```bash
# base64 a local image and embed it
B64=$(base64 -w0 ./pic.jpg)
curl "$OWUI_URL/api/chat/completions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d "{
    \\"model\\": \\"qwen2.5vl:3b\\",
    \\"messages\\": [{
      \\"role\\": \\"user\\",
      \\"content\\": [
        {\\"type\\": \\"text\\", \\"text\\": \\"What is in this image?\\"},
        {\\"type\\": \\"image_url\\", \\"image_url\\": {\\"url\\": \\"data:image/jpeg;base64,$B64\\"}}
      ]
    }]
  }"
```

- Public URL instead of base64: `"image_url": {"url": "<https://example.com/pic.jpg>"}`
- Multiple images: add more `image_url` objects to the `content` array.
- Formats: jpeg, png, gif, webp.

### 2. Files / Documents (RAG) — upload, then reference by id

For PDF / txt / docx / csv / md. Open-WebUI extracts text, chunks it, embeds it (`all-MiniLM-L6-v2`), and injects relevant chunks as context. Works with any chat model.

```bash
# 1. Upload — returns {"id": "<file_id>", ...}
FID=$(curl -s "$OWUI_URL/api/v1/files/" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -F "file=@./document.pdf" \\
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 2. Chat referencing the uploaded file
curl "$OWUI_URL/api/chat/completions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d "{
    \\"model\\": \\"llama3.1:8b\\",
    \\"messages\\": [{\\"role\\": \\"user\\", \\"content\\": \\"Summarize the document.\\"}],
    \\"files\\": [{\\"type\\": \\"file\\", \\"id\\": \\"$FID\\"}]
  }"
```

- Whole knowledge collection instead of one file: `"files": [{"type": "collection", "id": "<knowledge_id>"}]`
- A scanned image you want OCR'd → upload via this file route, not the image/vision route.

### 3. Audio — separate endpoints

```bash
# Speech-to-text (transcription)
curl "$OWUI_URL/api/v1/audio/transcriptions" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -F "file=@./audio.mp3"

# Text-to-speech (returns audio bytes)
curl "$OWUI_URL/api/v1/audio/speech" \\
  -H "Authorization: Bearer $OWUI_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"input": "Hello world", "voice": "alloy"}' \\
  --output speech.mp3
```

### Quick comparison

| Input | How sent | Model needed | Processing |
| --- | --- | --- | --- |
| Image | inline base64 / URL in `content` array | vision (`qwen2.5vl:3b`) | direct to model, no embedding |
| File/doc | upload → reference `id` in `files` | any chat model | chunked + embedded (RAG) |
| Audio | multipart to audio endpoints | STT/TTS engine | transcribe / synthesize |

---

## Ollama Native API (no auth, internal use)

Direct to the engine. No key, no Open-WebUI features. Use only on a trusted network.

### List models

```bash
curl <http://192.168.0.106:11434/api/tags>
```

### Chat

```bash
curl <http://192.168.0.106:11434/api/chat> -d '{
  "model": "llama3.1:8b",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}'
```

### Generate (single prompt)

```bash
curl <http://192.168.0.106:11434/api/generate> -d '{
  "model": "llama3.1:8b",
  "prompt": "Why is the sky blue?",
  "stream": false
}'
```

### Embeddings

```bash
curl <http://192.168.0.106:11434/api/embeddings> -d '{
  "model": "llama3.1:8b",
  "prompt": "text to embed"
}'
```

### Currently loaded models (GPU/CPU split)

```bash
curl <http://192.168.0.106:11434/api/ps>
```

> Note: Ollama is currently bound to `172.17.0.1:11434` (Docker bridge only). For LAN access, change the systemd override to `0.0.0.0:11434`:
> 
> 
> ```
> # /etc/systemd/system/ollama.service.d/override.conf
> [Service]
> Environment="OLLAMA_HOST=0.0.0.0:11434"
> ```
> 
> then `sudo systemctl daemon-reload && sudo systemctl restart ollama`.
> 

---

## SDK Examples

### Python (OpenAI SDK → Open-WebUI)

```python
from openai import OpenAI

client = OpenAI(
    base_url="<https://ollamallm.tools.thefusionapps.com/api>",
    api_key="sk-xxxxxxxxxxxxxxxx",
)

resp = client.chat.completions.create(
    model="llama3.1:8b",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

### Python (Ollama native)

```python
import requests

r = requests.post("<http://192.168.0.106:11434/api/chat>", json={
    "model": "llama3.1:8b",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": False,
})
print(r.json()["message"]["content"])
```

---

## Endpoint Status (last verified 2026-06-05)

| Endpoint | Status |
| --- | --- |
| `GET <https://ollamallm.tools.thefusionapps.com/`> | 200 |
| `GET <https://ollamallm.tools.thefusionapps.com/health`> | 200 |
| `POST /api/chat/completions` (no key) | 401 (auth required — expected) |
| `GET <http://172.17.0.1:11434/api/tags`> | 200 (models listed) |

## Security Notes

- The public domain requires an API key for all `/api/*` data endpoints. Keep keys secret.
- The raw Ollama port (11434) has **no authentication** — never expose it to the public internet. Keep it bound to the Docker bridge or LAN only.
- Rotate API keys from Settings → Account → API Keys if leaked.