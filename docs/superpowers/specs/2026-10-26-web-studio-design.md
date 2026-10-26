# agy2api Web Studio — Design Specification

## 1. Intent & Purpose
`agy2api` provides high-performance local REST endpoints bridging the Google Antigravity CLI to OpenAI and Gemini API standards. This feature adds a built-in, zero-dependency web interface ("agy2api Web Studio") served directly by FastAPI. It enables users to chat, test models, stream real-time tokens, inspect reasoning/thinking steps, attach multimodal media (images, PDFs, audio, video), analyze YouTube URLs, and view developer inspection data (cURL, JSON payloads, latency, token usage), with complete accessibility for screen readers (TalkBack & Jieshuo / CSR).

## 2. Goals & Non-Goals
### Goals:
1. **Zero External Build Tooling**: Single cohesive web client (HTML5, CSS, modern Vanilla JS) stored in `web/` and served via FastAPI (`/` and `/static`).
2. **Full API Parity**:
   - Model switching (`gemini-3.8-flash-low/med/high`, `claude-sonnet-4.6`, `claude-opus-4.6`, `gpt-oss-120b`).
   - Reasoning effort controls (low, medium, high).
   - Real-time Server-Sent Events (SSE) token streaming.
   - Expandable Thinking/Reasoning block.
   - Multimodal file uploads (Images, Audio, Video, PDF) encoded as Base64 data URLs.
   - YouTube URL support.
   - System instruction input.
   - Auth token storage (Bearer token).
3. **Developer Inspector**: Drawer displaying cURL command, raw request/response JSON, latency in milliseconds, and token usage breakdown.
4. **Accessibility First (WCAG 2.2 AA+)**:
   - Semantic HTML (`<main>`, `<nav>`, `<article>`, `<header>`, `<section>`).
   - Live region (`role="status"` / `aria-live="polite"`) for streaming status.
   - All icon buttons accompanied by explicit `aria-label` and visible text where appropriate.
   - Full keyboard navigation (`Tab`, `Shift+Tab`, `Ctrl+Enter` to submit, `Esc` to dismiss drawers).
   - High contrast OLED dark palette (`#0F172A` background, `#1B2336` cards, `#22C55E` accent, `#F8FAFC` text).

### Non-Goals:
- Adding npm/Node.js dependencies or build steps.
- Storing chats in external databases (browser localStorage will retain active session history locally).

## 3. Architecture & File Layout
- `web/index.html`: Semantic markup, accessible dialogs, chat stream viewport.
- `web/style.css`: Clean responsive CSS using UI/UX Pro Max OLED design tokens, dark mode, high contrast, mobile-friendly flex/grid.
- `web/app.js`: Modern vanilla JavaScript managing state, SSE stream reading via `fetch` ReadableStream, Base64 file conversion, model catalog fetching, and dev tools.
- `server.py`: Mounts `web/` at `/static` and serves `index.html` at `GET /` and `GET /ui`.
- `run_tests.py`: Unit and integration tests verifying `GET /`, `GET /ui`, and static asset serving.
