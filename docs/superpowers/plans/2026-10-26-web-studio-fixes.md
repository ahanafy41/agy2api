# agy2api Web Studio Responsiveness & Accessibility Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the unresponsiveness and screen blockage in agy2api Web Studio caused by CSS `[hidden]` override, add mobile-friendly keyboard submission, support dual OpenAI/Gemini response parsing, and bulletproof client storage against permission crashes.

**Architecture:** Enforce CSS display isolation rules (`[hidden] { display: none !important; }` and `dialog:not([open]) { display: none !important; }`) to prevent developer drawers and dialogs from hijacking mobile touch coordinates (`z-index: 100`). In JavaScript, encapsulate `localStorage` into a resilient in-memory fallback, improve keyboard event handling for mobile soft keyboards (Enter vs Shift+Enter), and expand the response decoder to support both OpenAI `choices` and Gemini `candidates` formats.

**Tech Stack:** HTML5, CSS3, Modern Vanilla JavaScript (ES2022), Python 3.10+ (FastAPI, unittest).

**Spec:** `docs/superpowers/specs/2026-10-26-web-studio-design.md`

## Global Constraints

- Zero external npm/build tool dependencies; standalone web assets in `web/`.
- Strict WCAG 2.2 AA accessibility: visible focus rings, aria-labels, TalkBack & Jieshuo (CSR) screen reader compatibility.
- Never block mobile touch targets; drawers and dialogs must consume zero pointer events when closed.
- Preserve all existing 40 tests in `run_tests.py` with zero regressions.

## Review Focus

1. Touch & Pointer Collision: Inspector drawer or dialogs never intercept clicks when closed (`elementFromPoint` must hit the textarea and send button).
2. Mobile Keyboards: Enter key sends message on desktop while Shift+Enter creates a newline; mobile soft keyboard submit button triggers send cleanly.
3. Dual API Decoding: Switching to Gemini mode (`/v1beta/models/...:generateContent`) renders text cleanly instead of an empty message bubble.
4. Storage Resilience: Incognito or restricted WebView where `localStorage` throws `SecurityError` must continue functioning via memory fallback.
5. Screen Reader Politeness: `#sr-announcer` updates when generation starts, streams, completes, or encounters an error without repetitive verbal chatter.

---

### Task 1: CSS Hardening & Hidden Display Invariants

**Files:**
- Modify: `web/style.css`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: CSS custom properties and existing layout rules in `web/style.css`.
- Produces: CSS rules ensuring elements with the `hidden` attribute or unopened dialogs are strictly `display: none !important;` and `pointer-events: none !important;`.

- [ ] **Step 1: Write the failing test for CSS hidden invariants in `run_tests.py`**

Add `test_css_hidden_invariants()` to `WebStudioEndpointTests` in `run_tests.py`:
```python
def test_css_hidden_invariants(self) -> None:
    css_path = Path(__file__).resolve().parent / "web" / "style.css"
    self.assertTrue(css_path.exists())
    css_content = css_path.read_text(encoding="utf-8")
    self.assertIn("[hidden]", css_content)
    self.assertIn("display: none !important", css_content)
    self.assertIn("dialog:not([open])", css_content)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest run_tests.py -k test_css_hidden_invariants`
Expected: FAIL (assertion error because `[hidden]` or `dialog:not([open])` rules are missing).

- [ ] **Step 3: Implement CSS hidden rules in `web/style.css`**

Add top-level display isolation reset in `web/style.css` right after the CSS variables / reset:
```css
/* Display isolation invariants: prevent CSS class rules from overriding hidden state */
[hidden] {
    display: none !important;
}

dialog:not([open]) {
    display: none !important;
}

.inspector-drawer[hidden] {
    display: none !important;
    pointer-events: none !important;
    visibility: hidden !important;
}
```
Also ensure `.btn[hidden]` and `.attachments-bar[hidden]` do not render.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest run_tests.py -k test_css_hidden_invariants`
Expected: PASS.

- [ ] **Step 5: Verify mobile viewport bounds with headless script**

Verify that on mobile viewport (412x915), `document.elementFromPoint(centerX, centerY)` for `#prompt-input` hits `TEXTAREA` and not `DIV.tab-panels`.

- [ ] **Step 6: Commit Task 1**

```bash
git add web/style.css run_tests.py
git commit -m "fix(web): enforce CSS hidden invariants to prevent inspector drawer from blocking mobile touch targets"
```

---

### Task 2: Mobile Input, Keyboard & Safe Storage

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: DOM elements `#prompt-input`, `#chat-form`, `#send-btn`, `#stop-btn`.
- Produces: Safe storage helper `safeStorage`, keyboard shortcut handler (Enter sends, Shift+Enter adds newline), and button visibility toggling.

- [ ] **Step 1: Write test for safe storage and client script contract in `run_tests.py`**

Add `test_js_client_contract()` to `WebStudioEndpointTests`:
```python
def test_js_client_contract(self) -> None:
    js_path = Path(__file__).resolve().parent / "web" / "app.js"
    self.assertTrue(js_path.exists())
    js_content = js_path.read_text(encoding="utf-8")
    self.assertIn("safeStorage", js_content)
    self.assertIn("shiftKey", js_content)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest run_tests.py -k test_js_client_contract`
Expected: FAIL with "safeStorage not in js_content".

- [ ] **Step 3: Implement safe storage helper in `web/app.js`**

Implement `safeStorage` at top of `web/app.js`:
```javascript
const memoryStore = {};
const safeStorage = {
    getItem(key, fallback = null) {
        try {
            const val = localStorage.getItem(key);
            return val !== null ? val : fallback;
        } catch {
            return key in memoryStore ? memoryStore[key] : fallback;
        }
    },
    setItem(key, value) {
        try {
            localStorage.setItem(key, String(value));
        } catch {
            memoryStore[key] = String(value);
        }
    }
};
```
Replace all direct `localStorage.getItem` and `localStorage.setItem` calls with `safeStorage`.

- [ ] **Step 4: Update keyboard submission logic in `web/app.js`**

Update the keydown listener on `elements.promptInput`:
```javascript
elements.promptInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        // Desktop or physical keyboard: Enter sends message, Shift+Enter adds line
        // Avoid sending during IME text composition
        if (e.isComposing) return;
        e.preventDefault();
        handleSubmit();
    }
});
```

- [ ] **Step 5: Verify button visibility toggling**

Ensure `elements.stopBtn.hidden = true` and `elements.sendBtn.hidden = false` cleanly control display without inline style conflicts.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m unittest run_tests.py -k test_js_client_contract`
Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add web/app.js run_tests.py
git commit -m "fix(web): add safeStorage wrapper and mobile-friendly Enter key handling"
```

---

### Task 3: Dual-Mode Protocol Decoder (Gemini & OpenAI)

**Files:**
- Modify: `web/app.js`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: API response payloads from `/v1/chat/completions` (OpenAI format) and `/v1beta/models/...:generateContent` (Gemini format).
- Produces: Normalized `fullAnswer` and `fullReasoning` strings extracted from either schema.

- [ ] **Step 1: Write test for dual-mode parsing logic in `run_tests.py`**

Add `test_js_dual_mode_decoder()` to `run_tests.py`:
```python
def test_js_dual_mode_decoder(self) -> None:
    js_path = Path(__file__).resolve().parent / "web" / "app.js"
    js_content = js_path.read_text(encoding="utf-8")
    self.assertIn("candidates", js_content)
    self.assertIn("parts", js_content)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest run_tests.py -k test_js_dual_mode_decoder`
Expected: FAIL (missing decoder checks for `candidates`).

- [ ] **Step 3: Implement normalized decoder in `web/app.js`**

In `executeChatRequest()`, update non-streaming response handling:
```javascript
const choice = json.choices && json.choices[0];
if (choice && choice.message) {
    fullAnswer = choice.message.content || '';
    fullReasoning = choice.message.reasoning_content || '';
} else if (json.candidates && json.candidates[0] && json.candidates[0].content) {
    const parts = json.candidates[0].content.parts || [];
    fullAnswer = parts.map(p => p.text || '').join('');
}
```
Also handle Gemini error format: `json.error?.message`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest run_tests.py -k test_js_dual_mode_decoder`
Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add web/app.js run_tests.py
git commit -m "fix(web): support Gemini format candidates parsing alongside OpenAI format"
```

---

### Task 4: Full Regression Testing & Verification

**Files:**
- Test: `run_tests.py`
- Verify: `web/index.html`, `web/style.css`, `web/app.js`

- [ ] **Step 1: Run full regression test suite**

Run: `python run_tests.py -v`
Expected: All 40+ tests pass with 0 failures and 0 errors.

- [ ] **Step 2: Run Node.js syntax & DOM integrity check**

Run:
```bash
node --check web/app.js
```
Expected: Exit code 0 with no syntax errors.

- [ ] **Step 3: Verify with headless browser simulation**

Run headless Chromium check verifying:
1. `#inspector-drawer` computed style is `display: none` when closed.
2. `#stop-btn` computed style is `display: none` before submission.
3. Element at input position is `#prompt-input`.

- [ ] **Step 4: Commit Task 4**

```bash
git add run_tests.py
git commit -m "test(web): verify complete responsive and accessibility fixes for web studio"
```
