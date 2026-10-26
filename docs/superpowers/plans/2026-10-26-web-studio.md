# agy2api Web Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and integrate "agy2api Web Studio", a comprehensive, zero-dependency, screen-reader accessible web application served directly by FastAPI, exposing all agy2api features (model selection, real-time streaming, thinking/reasoning inspection, multimodal media ingestion, YouTube processing, and developer inspection).

**Architecture:** A static single-page application (`web/index.html`, `web/style.css`, `web/app.js`) mounted in FastAPI at `/static` and served at `/` and `/ui`. Client handles SSE streaming via standard `fetch` + `ReadableStream`, multimodal encoding to Base64, token auth persistence, and developer inspection drawer.

**Tech Stack:** Python 3.10+ (FastAPI, Starlette StaticFiles), Vanilla Modern JavaScript (ES2022), Semantic HTML5 & CSS3 with OLED design tokens.

**Spec:** `docs/superpowers/specs/2026-10-26-web-studio-design.md`

## Global Constraints
- Zero external npm/build tool dependencies; standalone web assets stored in `web/`.
- Default port: 7862, host loopback by default.
- Strict WCAG 2.2 AA accessibility: visible focus rings, aria-labels on interactive elements, live regions for generation status, TalkBack/CSR friendly.
- Preserve all existing 37 tests in `run_tests.py` without regressions.

## Review Focus
1. Accessibility: Screen readers announce message completion, token stream status, and drawer openings properly.
2. Multimodal: Large file attachments or images do not crash browser client and encode cleanly to Base64.
3. SSE Streaming: Stream termination (`[DONE]`, network error, or cancel button) cleans up UI state reliably.
4. Auth handling: 401 Unauthorized errors surface clear feedback prompting user to update the Bearer token in settings.
5. Static mount fallback: Missing `web/` folder or root route gracefully handles errors without crashing server.

---

### Task 1: Server Endpoints & Static Mount

**Files:**
- Modify: `server.py`
- Test: `run_tests.py`

- [x] **Step 1: Write the failing tests in `run_tests.py`**
  Add `WebStudioEndpointTests` in `run_tests.py` asserting:
  - `GET /` returns 200 with HTML content type containing "agy2api Web Studio".
  - `GET /ui` redirects or returns 200 with HTML content type.
  - `GET /static/style.css` and `GET /static/app.js` return static assets.
- [x] **Step 2: Run tests to confirm failure**
  Run `python run_tests.py -v` and verify the new tests fail as expected.
- [x] **Step 3: Implement static mount & root routes in `server.py`**
  - Import `FileResponse` from `fastapi.responses` and `StaticFiles` from `fastapi.staticfiles`.
  - Mount directory `web/` to `/static`.
  - Add `@app.get("/")` and `@app.get("/ui")` serving `web/index.html`.
- [x] **Step 4: Run tests to confirm pass**
  Run `python run_tests.py -v` and verify all tests pass.
- [x] **Step 5: Git commit Task 1**
  Commit the route and test changes.

---

### Task 2: Semantic HTML Structure (`web/index.html`)

**Files:**
- Create: `web/index.html`

- [x] **Step 1: Create accessible HTML5 skeleton**
  - Implement semantic landmarks: `<header role="banner">`, `<main id="main-content">`, `<section aria-label="Chat">`, `<aside aria-label="API Inspector">`.
  - Add skip-to-content link: `<a href="#main-content" class="skip-link">تخطي إلى المحتوى الرئيسي</a>`.
  - Add header controls: Server health badge (`<span role="status" id="health-badge">`), Settings button (`aria-haspopup="dialog"`), Inspector toggle (`aria-expanded="false"`).
  - Add chat message list: `<div id="chat-messages" role="log" aria-live="polite" aria-relevant="additions text">`.
  - Add input bar: Multimodal attachment button (`<input type="file" id="media-input" hidden multiple accept="image/*,video/*,audio/*,.pdf">`), YouTube URL input, Prompt `<textarea id="prompt-input" rows="2" aria-label="اكتب رسالتك">`, and Send/Stop button (`#send-btn`).
  - Add Settings Modal (`<dialog id="settings-dialog">`) with Bearer Token input, API Endpoint toggle (OpenAI / Gemini), Temperature, System Prompt, and Clear Chat.
  - Add Developer Inspector drawer (`<aside id="inspector-drawer" hidden>`) with Tabs for cURL, Request JSON, Response JSON, and Metrics.
- [x] **Step 2: Verify HTML structure & accessibility**
  Verify all elements have labels, semantic tags, and valid ARIA attributes.

---

### Task 3: OLED Dark Mode CSS Design System (`web/style.css`)

**Files:**
- Create: `web/style.css`

- [x] **Step 1: Define CSS custom properties & base reset**
  - Set CSS variables: `--color-primary: #1E293B`, `--color-background: #0F172A`, `--color-card: #1B2336`, `--color-foreground: #F8FAFC`, `--color-accent: #22C55E`, `--color-border: #334155`, `--color-ring: #38BDF8`.
  - Ensure high contrast ratios (minimum 4.5:1 for body text, 3:1 for large text/icons).
  - Visible focus outlines (`:focus-visible` with `2px solid var(--color-ring)` and offset).
  - Respect `@media (prefers-reduced-motion: reduce)`.
- [x] **Step 2: Style chat layout and bubbles**
  - User messages: Distinct right-aligned bubble with subtle background.
  - Assistant messages: Full-width clear card with markdown styling, code block headers with copy buttons.
  - Thinking/Reasoning block: Accessible `<details class="reasoning-box"><summary class="reasoning-summary">خطوات التفكير (Reasoning)</summary>...</details>`.
  - Attachment preview chips (showing filename, size, remove button).
- [x] **Step 3: Style inspector drawer & settings dialog**
  - Drawer slide-in / backdrop overlay.
  - Responsive flexbox/grid layout optimized for mobile screens (375px+) and desktop monitors.

---

### Task 4: Interactive Client Application Logic (`web/app.js`)

**Files:**
- Create: `web/app.js`

- [x] **Step 1: State management & configuration persistence**
  - Load and save settings to `localStorage`: `apiKey` (default `"pwd"`), `selectedModel`, `reasoningEffort`, `systemPrompt`, `streamMode` (default `true`), `apiMode` (`openai` vs `gemini`).
  - Fetch models from `GET /v1/models` dynamically on load; populate select dropdown.
  - Live server health check (`GET /health`) with visual & screen-reader badge update.
- [x] **Step 2: Multimodal file handling & YouTube detection**
  - Convert selected files (images/PDF/audio/video) to Base64 data URLs via `FileReader`.
  - Detect YouTube links in prompt or attachment input.
  - Preview selected files with thumbnail / chip and remove button.
- [x] **Step 3: Real-time SSE streaming & chat orchestration**
  - Assemble OpenAI payload (`/v1/chat/completions`) or Gemini payload (`/v1beta/models/...`).
  - Send request with `fetch` and handle `ReadableStream` line by line.
  - Parse SSE `data: {...}` chunks, accumulate reasoning tokens into the `<details>` block, and content tokens into the message body.
  - Support aborting stream via `AbortController`.
  - Calculate latency (ms) and display token usage.
- [x] **Step 4: Developer Inspector updates & cURL generator**
  - Synthesize accurate `curl` command for current request.
  - Display raw request and response JSON in syntax-formatted `<pre><code>`.
- [x] **Step 5: Keyboard shortcuts & Screen reader announcements**
  - `Ctrl + Enter` / `Cmd + Enter` to send prompt.
  - `Escape` closes settings dialog and inspector drawer.
  - Live status updates in `role="status"` region ("جاري إرسال الطلب...", "جاري استلام الرد...", "اكتمل الرد بنجاح").

---

### Task 5: End-to-End Verification & Pre-Delivery Checklist

**Files:**
- Test: `run_tests.py`
- Verify: Live server check and static assets serving

- [x] **Step 1: Execute full automated test suite**
  Run `python run_tests.py -v` and ensure 100% pass (all 40 tests).
- [x] **Step 2: Verify in browser / HTTP GET checks**
  Verify `GET /`, `GET /static/style.css`, `GET /static/app.js` return HTTP 200 with appropriate content.
- [x] **Step 3: Screen reader & UI/UX Pro Max validation**
  Verify all checklist items: zero emojis used as functional icons, explicit aria-labels, visible focus rings, high contrast.
- [x] **Step 4: Final commit and clean status**
