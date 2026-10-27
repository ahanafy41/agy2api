# AGENTS.md — Developer & AI Agent Guidelines for agy2api

> This document serves as the single source of truth for autonomous AI agents, coding assistants, and human contributors developing or maintaining the `agy2api` codebase.

---

## 1. Project Purpose & Core Architecture

`agy2api` is a high-performance local API bridge that exposes the logged-in official Google Antigravity CLI (`agy`) through standard REST endpoints:
- **OpenAI Compatible**: `POST /v1/chat/completions` (with reasoning effort, SSE streaming, and fake streaming).
- **Google Gemini Compatible**: `POST /v1beta/models/{model}:generateContent` (for zero-config drop-in use with mobile apps, local automation scripts, and Gemini client SDKs).
- **Web Studio & UI Playground**: `GET /` and `GET /ui` (accessible Arabic RTL web studio with model picker, reasoning effort toggle, live token counter, and multimodal upload).
- **Static Assets**: `GET /static/*` (serves UI assets from `web/`).

### Architecture Diagram
```
                     +------------------------------------------------+
                     |         FastAPI Server (server.py)             |
                     |  Port: 7862 (default) | Auth: Bearer Token     |
                     +-----------------------+------------------------+
                                             |
                   +-------------------------+-------------------------+
                   |                                                   |
                   v (Default: Stateless / Warm)                       v (Fallback / Stateful)
    +------------------------------------+                 +-------------------------------+
    | Isolated Worker                    |                 |   CLI Runner (agy_runner.py)  |
    | (isolated_session.py)              |                 |   - One-shot `agy --print`    |
    | - stream-json pipe IPC             |                 |   - SQLite DB step extractor  |
    | - Native Real-time Token Streaming |                 |   - Protobuf wire decoder     |
    | - Sub-2.5s turn latency            |                 |   - SessionStore LRU cache    |
    | - Context Bloat Protection         |                 +-------------------------------+
    | - Idle Auto-Recycle (600s)         |
    +------------------------------------+
```

---

## 2. Directory Structure & Module Responsibilities

| File / Folder | Role & Single Responsibility |
| :--- | :--- |
| `server.py` | FastAPI application, route handlers, multimodal media ingestion, and error responses. |
| `youtube_handler.py` | Two-tier hybrid YouTube ingestion: instant transcript extraction and ephemeral micro-video visual inspection. |
| `isolated_session.py` | Manages persistent `agy` CLI processes in `stream-json` mode with real-time token streaming and auto-recycling. |
| `agy_runner.py` | Manages cold subprocess runs, SQLite database discovery, and Windows process tree guards. |
| `conversation_reader.py` | Reads step 15 / status 3 results from SQLite databases created by `agy`. |
| `proto_decoder.py` | Pure-Python wire protobuf parser for decoding `steps.step_payload`. Zero external dependencies. |
| `config.py` | Configuration via environment variables, Pydantic settings, and model catalog / alias resolution. |
| `models.py` | Pydantic data schemas for OpenAI request/response models. |
| `session_store.py` | In-memory LRU session mapping for stateful multi-turn conversations. |
| `fake_stream.py` | Formats completed results into SSE event chunks with heartbeat pulses. |
| `run_tests.py` | Zero-dependency standard library (`unittest`) regression test suite. |
| `launch.sh` | Linux / Termux background daemon launcher with automatic port verification. |
| `start.sh` | Linux / Termux interactive launcher with dependency and auth token checks. |
| `stop.sh` | Clean process-group killer (`SIGTERM` / `SIGKILL`) to prevent orphaned zombie processes. |
| `start.bat` | Windows launcher with virtual environment auto-detection. |
| `watchdog.sh` | Health-check watchdog script for auto-restarting dead server processes. |
| `web/` | Accessible Arabic RTL Web Studio interface (`index.html`, `app.js`, `style.css`) for prompt testing, live token streaming, and multimodal inspection. |
| `media_cache/` | Runtime scratch folder for temporary base64 decoded images and media files (gitignored). |
| `isolated_home/` | Sandboxed home directory for warm workers to avoid polluting user configuration (gitignored). |

---

## 3. Mandatory Engineering & Security Invariants

When modifying this repository, AI agents and contributors **MUST** uphold the following rules:

1. **Security & Secrets**:
   - Never hardcode API keys, passwords, or tokens in source code.
   - Never read or modify the user's real `~/.gemini/antigravity-cli/antigravity-oauth-token` directly; warm workers copy this token into `isolated_home/`.
   - Never bind to `0.0.0.0` unless `AGY2API_ALLOW_REMOTE=1` is explicitly set. Default must remain `127.0.0.1`.

2. **Process Group Isolation & Zombie Prevention**:
   - Any subprocess spawned MUST use `start_new_session=True` (on POSIX) or `CREATE_NEW_PROCESS_GROUP` (on Windows).
   - Terminations must kill the entire process group (`os.killpg(pgid, signal.SIGTERM)`) to prevent orphaned child processes (`agy.va39`).

3. **Context Cleanliness in Warm Worker**:
   - The warm worker is stateless by default. To prevent context bloat and latency degradation, the worker rotates sessions automatically after every 1–2 turns using a pre-warmed standby instance.
   - Brain directories (`~/.gemini/antigravity-cli/brain/<cid>`) must be deleted upon worker termination.

4. **Media Cache Lifecycle**:
   - Decoded Base64 media stored in `media_cache/` must be ephemeral. The cleanup routine `_cleanup_old_media_cache()` must be executed to prevent storage accumulation.

---

## 4. Developer & Testing Cheat-Sheet

### Run Test Suite
```bash
python run_tests.py -v
```
*Expected: 45 tests pass with 0 failures (1 skipped).*

### Launch Background Daemon (Termux / Linux)
```bash
./launch.sh
```

### Stop Background Daemon
```bash
./stop.sh
```

### Check Server Health
```bash
curl -s http://127.0.0.1:7862/v1/models -H "Authorization: Bearer pwd"
```

### Benchmark Latency
```bash
time curl -s -X POST http://127.0.0.1:7862/v1/chat/completions \
  -H "Authorization: Bearer pwd" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash-low","messages":[{"role":"user","content":"Ping"}]}'
```

---

## 5. Coding Standards & Conventions

- **Language & Runtime**: Python 3.10+ (using `from __future__ import annotations`).
- **Typing**: Strict type annotations for all function parameters and return types.
- **Dependencies**: Keep external dependencies minimal (FastAPI, Uvicorn, Pydantic, Requests). Do not add heavy frameworks.
- **Git Commits**: Follow Conventional Commits format (`feat:`, `fix:`, `perf:`, `docs:`, `refactor:`, `test:`).
