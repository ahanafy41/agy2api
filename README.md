# agy2api (Enhanced High-Performance Edition) 🚀

> High-performance, Multimodal OpenAI & Google Gemini API Server powered by the logged-in official Google Antigravity CLI (`agy`).

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Android%20(Termux)%20%7C%20macOS%20%7C%20Windows-green)](#quick-start)
[![API](https://img.shields.io/badge/API-OpenAI%20%7C%20Google%20Gemini-orange)](#api-endpoints)

[📖 **التوثيق باللغة العربية (Arabic Documentation)**](README_AR.md)

---

## 🌟 Key Innovations & Enhancements

Unlike standard CLI wrappers that invoke cold processes on every turn (incurring 15–20s startup overhead), this enhanced edition introduces a production-ready, low-latency architecture:

### 1. ⚡ Ultra-Fast Warm Worker Engine (`warm_worker.py`)
- **Sub-2.5s Turn Latency**: Retains official `agy` CLI processes hot in memory using native `--input-format stream-json --output-format stream-json`.
- **Proactive Standby Rotation (Zero Context Bloat)**: Automatically pre-warms a standby worker in the background and rotates sessions seamlessly in 0.0s, eliminating the conversation accumulation that causes long-term latency degradation.
- **Auto-Recycling & Idle Monitor**: Recycles inactive sessions after 90 seconds of idle time to conserve system RAM.
- **Zero Zombie Processes**: Robust process-group sandboxing (`start_new_session=True` & `os.killpg`) guarantees clean process lifecycles.

### 2. 🖼️ Native Multimodal Support (Images, PDFs, Videos & YouTube)
- **Base64 Images**: Decodes and feeds image inputs (JPEG, PNG, WEBP) directly to Antigravity vision models.
- **PDF Documents & Books**: Extracts and analyzes local PDF documents.
- **Local Audio & Video**: Inspects media files with proper MIME type classification (MP4, MKV, MP3, WAV, etc.).
- **YouTube Links**: Ingests direct YouTube links for video summarization and Q&A.
- **Zero-Footprint Media Cache (`media_cache/`)**: Automatically wipes temporary media files immediately upon response delivery.

### 3. 📱 Mobile & Termux Native (Android, Linux & Windows)
- Dedicated Linux / Termux background daemon launcher (`launch.sh`).
- Interactive CLI startup assistant (`start.sh`).
- Deterministic process killer (`stop.sh`) to prevent orphaned processes on mobile devices.
- Windows batch launcher (`start.bat`) with automatic venv detection.

### 4. 🌐 Dual-Protocol Compatibility (OpenAI + Google Gemini)
- **OpenAI Compatible**: `POST /v1/chat/completions` (supporting reasoning effort, fake streaming, and standard OpenAI SDKs).
- **Google Gemini Compatible**: `POST /v1beta/models/{model}:generateContent` (enables Android apps and screen reader extensions to replace the official Google endpoint with zero code changes).

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
git clone https://github.com/kamalyaser31/agy2api.git
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

## 🧪 Testing

Run the included regression and verification test suite:

```bash
python run_tests.py -v
```

All 21 unit tests verify Pydantic models, CLI wrappers, protocol encoders, fake-stream helpers, and stateful session logic.

---

## ⚖️ License & Compliance

- Licensed under **Apache-2.0**.
- **Unofficial Personal Tool**: This software is not affiliated with, endorsed by, or supported by Google.
- All requests leverage the user's own authenticated Google Antigravity quota.

---

*Authored & Enhanced with passion for high-speed accessibility and mobile AI automation.*
