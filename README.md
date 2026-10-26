# agy2api (Enhanced High-Performance Edition) 🚀

> High-performance, Multimodal OpenAI & Google Gemini API Server powered by the logged-in official Google Antigravity CLI (`agy`).

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Android%20(Termux)%20%7C%20macOS%20%7C%20Windows-green)](#quick-start)
[![API](https://img.shields.io/badge/API-OpenAI%20%7C%20Google%20Gemini-orange)](#api-endpoints)

---

## 🌟 Key Innovations & Enhancements

Unlike standard CLI wrappers that invoke cold processes on every turn (incurring 15–20s startup overhead), this enhanced edition introduces a production-ready, low-latency architecture:

### 1. ⚡ Ultra-Fast Worker Engine (`isolated_session.py`)
- **Sub-2.5s Turn Latency & Real-Time Token Streaming**: Retains official `agy` CLI processes hot in memory using native `--input-format stream-json --output-format stream-json` and streams tokens live as they are generated.
- **Context Bloat & Cross-Talk Protection**: Automatically recycles sessions and cleans conversation databases to prevent $O(N^2)$ quadratic token multiplication.
- **Auto-Recycling & Idle Monitor**: Recycles inactive sessions after idle timeout to conserve system RAM and battery.
- **Zero Zombie Processes**: Robust process-group sandboxing (`start_new_session=True` & `os.killpg`) guarantees clean process lifecycles.

### 2. 🖼️ Native Multimodal Support (Images, PDFs, Videos & YouTube)
- **Base64 Images**: Decodes and feeds image inputs (JPEG, PNG, WEBP) directly to Antigravity vision models.
- **PDF Documents & Books**: Extracts and analyzes local PDF documents.
- **Local Audio & Video**: Inspects media files with proper MIME type classification (MP4, MKV, MP3, WAV, etc.).
- **Two-Tier YouTube Ingestion Engine**: Instant metadata & spoken transcript extraction via `yt-dlp` for text queries; automatic ephemeral 144p micro-video download for blind/visual scene descriptions with immediate post-response cleanup.
- **Zero-Footprint Media Cache (`media_cache/`)**: Automatically wipes temporary media files immediately upon response delivery.

### 3. 📱 Mobile & Termux Native (Android, Linux & Windows)
- Dedicated Linux / Termux background daemon launcher (`launch.sh`).
- Interactive CLI startup assistant (`start.sh`).
- Deterministic process killer (`stop.sh`) to prevent orphaned processes on mobile devices.
- Windows batch launcher (`start.bat`) with automatic venv detection.

### 4. 🌐 Dual-Protocol Compatibility (OpenAI + Google Gemini)
- **OpenAI Compatible**: `POST /v1/chat/completions` (supporting reasoning effort, fake streaming, and standard OpenAI SDKs).
- **Google Gemini Compatible**: `POST /v1beta/models/{model}:generateContent` (enables Android apps and screen reader extensions to replace the official Google endpoint with zero code changes).

### 5. 🎨 Accessible Web Studio UI Playground (`web/`)
- **Zero-Dependency Web Interface**: Built-in responsive web dashboard served directly at `http://127.0.0.1:7862/` and `http://127.0.0.1:7862/ui`.
- **Accessibility-First Design**: Full Arabic RTL layout with dedicated screen reader live announcer (`aria-live="polite"`), skip-to-content anchors, accessible form labels, and complete keyboard navigation.
- **Dynamic Model & Thinking Selector**: Switch dynamically between Gemini 3.8 Flash (Low, Medium, High), Claude Sonnet/Opus 4.6 (Thinking), and GPT-OSS 120B with fine-grained reasoning effort control (Low, Medium, High).
- **Real-Time Token & Latency Metrics**: Live token streaming counter, speed benchmarking (tokens/sec), and turn latency timers.
- **Multimodal File Attachments**: Drag-and-drop or select local images, documents, and media with live previews.

---

## 📋 Prerequisites

This wrapper does **not** handle Google authentication. You must configure the official CLI first:

1. **Install official `agy` CLI**: Ensure `agy` is installed and accessible in your `$PATH`.
2. **Authenticate with Google**: Run `agy` and complete the initial OAuth sign-in.
3. **Python 3.10+**: Ensure Python is installed (`pkg install python` on Termux).

---

## 🚀 Quick Start

### 1. Installation

```bash
git clone https://github.com/ahanafy41/agy2api.git
cd agy2api
pip install -r requirements.txt
```

### 2. Running on Linux / Android (Termux) / macOS

#### Option A: Run as a Background Daemon (Recommended for daily use)
```bash
chmod +x launch.sh stop.sh start.sh
./launch.sh
```
*The server launches silently in the background on `http://127.0.0.1:7862`. You can safely close your terminal session.*

#### Option B: Interactive Mode
```bash
./start.sh
```

#### Stopping the Server
```bash
./stop.sh
```

### 3. Running on Windows

Double-click `start.bat` or run:
```cmd
start.bat
```

---

## 📡 API Endpoints & Examples

Default URL: `http://127.0.0.1:7862`  
Default Bearer Key: `pwd`

### 0. 🎨 Web Studio (Interactive Browser Dashboard)
Open in any browser (Chrome, Kiwi, Firefox) to chat, stream tokens, test reasoning effort, and attach files visually:
- **URL**: `http://127.0.0.1:7862/` or `http://127.0.0.1:7862/ui`
- **Static Assets**: `http://127.0.0.1:7862/static/` (served from `web/`)

### 1. Standard OpenAI Text Completion
```bash
curl -X POST http://127.0.0.1:7862/v1/chat/completions \
  -H "Authorization: Bearer pwd" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-low",
    "messages": [
      {"role": "user", "content": "Explain quantum computing in one sentence."}
    ]
  }'
```

### 2. Multimodal Image Analysis (Base64)
```bash
curl -X POST http://127.0.0.1:7862/v1/chat/completions \
  -H "Authorization: Bearer pwd" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-low",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "Describe the contents of this image in detail."},
          {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg..."}
          }
        ]
      }
    ]
  }'
```

### 3. Google Gemini Native Endpoint (`generateContent`)
```bash
curl -X POST http://127.0.0.1:7862/v1beta/models/gemini-3.8-flash-low:generateContent \
  -H "Authorization: Bearer pwd" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "parts": [
          {"text": "Hello, world!"}
        ]
      }
    ]
  }'
```

---

## ⚙️ Configuration Reference

| Environment Variable | Description | Default |
| :--- | :--- | :--- |
| `PORT` | Local HTTP binding port | `7862` |
| `HOST` | Local binding interface | `127.0.0.1` |
| `AGY2API_KEY` | Bearer token authorization password | `pwd` |
| `AGY2API_WARM_WORKER` | Keep warm in-memory CLI worker active | `1` (true) |
| `AGY2API_MAX_CONCURRENCY` | Maximum concurrent execution requests | `3` |
| `AGY_TIMEOUT` | Per-request timeout budget in seconds | `300` |
| `AGY2API_STATEFUL` | Stateful conversation mode (preserves session DB) | `0` (false) |
| `AGY2API_EXPOSE_REASONING` | Include reasoning output in completion chunks | `true` |

---

## 📱 Mobile & Android Apps Integration

`agy2api` acts as a seamless local gateway for mobile automation, Android applications, and custom client scripts:

1. **Host**: `127.0.0.1`
2. **Port**: `7862`
3. **Bearer API Key**: `pwd` (or your custom `AGY2API_KEY`)
4. **Endpoint**: Select either OpenAI chat format (`/v1/chat/completions`) or native Google Gemini format (`/v1beta/models/{model}:generateContent`).
5. **Recommended Models**:
   - `gemini-3.8-flash-low`: Sub-second to 2s latency, ideal for voice dictation and real-time typing corrections.
   - `gemini-3.8-flash-medium`: Balanced model with high grammatical and dialectal precision.

---

## 🧪 Testing

Run the included regression and verification test suite:

```bash
python run_tests.py -v
```

All 43 unit tests verify Pydantic models, CLI wrappers, protocol encoders, fake-stream helpers, stateful session logic, multimodal caching, and Web Studio static/UI routes.

---

## ⚖️ License & Compliance

- Licensed under **Apache-2.0**.
- **Unofficial Personal Tool**: This software is not affiliated with, endorsed by, or supported by Google.
- All requests leverage the user's own authenticated Google Antigravity quota.

---

*Authored & Enhanced with passion for high-speed accessibility and mobile AI automation.*
