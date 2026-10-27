# Code Refactoring & Performance Optimization Plan for agy2api

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `agy2api` for optimal performance, memory efficiency on Termux/Android, clean single-responsibility architecture, and zero-defect regression parity.

**Architecture:**
- Target I/O throttling and $O(1)$ data structures to conserve ARM CPU and mobile flash memory.
- Enforce strict behavioral invariance so all existing API contracts and test suites pass 100%.
- Streamline complex functions (`gemini_generate_content`, `_stream_response`, `read_response`) into modular, testable components.

**Tech Stack:** Python 3.10+, FastAPI, Starlette, SQLite (URI read-only), `collections.OrderedDict`.

**Spec:** Codebase audit adhering to `code-refactor-pro` 4-phase protocol.

## Global Constraints
- **Strict Behavioral Invariance**: External REST APIs (`/v1/chat/completions`, `/v1beta/models/...:generateContent`, `/ui`, `/v1/models`) must retain identical behavior.
- **Zero New External Dependencies**: Standard library + existing dependencies (FastAPI, Uvicorn, Pydantic, Requests) only.
- **100% Test Pass Rate**: All unit tests in `run_tests.py` must pass with zero failures.

## Review Focus
1. Ephemeral media files in `media_cache/` must be reliably unlinked when real-time streaming completes or disconnects.
2. LRU eviction in `SessionStore` must strictly respect `protected` conversation IDs.
3. SQLite read-only connection mode must not fail when reading WAL or legacy databases.
4. Model effort suffix resolution must correctly match `(Low)`, `(Medium)`, `(High)`, and `(Thinking)`.
5. YouTube URL detection in Gemini parts must not perform redundant nested scans.

---

### Task 1: Optimize Regex Compilation & Top-Level Imports in `config.py`

**Files:**
- Modify: `config.py:1-20, 93-112`
- Test: `run_tests.py` (ModelResolutionTests)

- [x] **Step 1: Inspect failing benchmark or verify test suite**
Run: `python -m unittest run_tests.ModelResolutionTests`
Expected: PASS

- [x] **Step 2: Move `import re` to top-level and compile `_EFFORT_SUFFIX_RE` in `config.py`**
Replace runtime `import re` and `re.sub(r"-(low|medium|high)$", "", canonical)` with module-level pre-compiled regex:
```python
_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high)$", re.IGNORECASE)
```

- [x] **Step 3: Run tests to verify model resolution behavior is preserved**
Run: `python -m unittest run_tests.ModelResolutionTests`
Expected: PASS

---

### Task 2: Eliminate Redundant Disk I/O & Optimize MIME Map in `server.py`

**Files:**
- Modify: `server.py:80-92, 546-590`
- Test: `run_tests.py` (MultimodalAndMediaCacheTests)

- [x] **Step 1: Throttle `_cleanup_old_media_cache` in `server.py`**
Add timestamp cooldown (`_last_media_cleanup: float = 0.0`) so directory iteration only executes at most once every 60 seconds during media saves, preventing I/O storms on batch uploads.

- [x] **Step 2: Replace `if/elif` MIME chain with static dictionary `_MIME_TO_EXT`**
Implement $O(1)$ dictionary lookup for MIME types mapping to file extensions (`.jpg`, `.png`, `.webp`, `.pdf`, `.mp4`, `.mp3`, `.wav`, etc.).

- [x] **Step 3: Run tests to verify media decoding and caching**
Run: `python -m unittest run_tests.MultimodalAndMediaCacheTests`
Expected: PASS

---

### Task 3: Fix Ephemeral Resource Leak in `_stream_response` (`server.py`)

**Files:**
- Modify: `server.py:777-872`
- Test: `run_tests.py` (BackgroundJanitorAndEphemeralTests)

- [x] **Step 1: Write regression test verifying cleanup on real-time stream completion**
Add test in `run_tests.py` asserting that ephemeral files are deleted even when `can_use_worker` stream branch finishes.

- [x] **Step 2: Wrap entire `_stream_response` in an outer `try...finally` block**
Ensure that `finally: if cleanup_files: ...` unconditionally executes upon generator exit, regardless of whether real-time stream or guarded fallback was used.

- [x] **Step 3: Run tests to verify ephemeral cleanup**
Run: `python -m unittest run_tests.BackgroundJanitorAndEphemeralTests`
Expected: PASS

---

### Task 4: Decompose `gemini_generate_content` and Eliminate $O(N^2)$ Scans (`server.py`)

**Files:**
- Modify: `server.py:593-776`
- Test: `run_tests.py` (GeminiEndpointTests)

- [x] **Step 1: Extract helper `_extract_gemini_multimodal_prompt`**
Split the 185-line handler into a clean, reusable transformation helper that parses `system_instruction`, `contents`, `parts`, `inline_data`, and `file_data`.

- [x] **Step 2: Optimize YouTube duplicate detection from $O(N^2)$ to $O(N)$**
Pre-collect `file_data` URIs into a `set` once, rather than iterating all contents and parts inside a nested generator for every text part.

- [x] **Step 3: Run Gemini endpoint tests**
Run: `python -m unittest run_tests.GeminiEndpointTests`
Expected: PASS

---

### Task 5: Refactor `SessionStore` to $O(1)$ LRU with `OrderedDict` (`session_store.py`)

**Files:**
- Modify: `session_store.py:47-96`
- Test: `run_tests.py` (SessionStoreTests)

- [x] **Step 1: Replace raw dict with `collections.OrderedDict` in `SessionStore`**
Use `OrderedDict` to maintain LRU access order. On `lookup`, call `self._sessions.move_to_end(best.conversation_id)`.

- [x] **Step 2: Optimize `_evict` to pop least recently used entries from front**
Evict from front in $O(1)$ while respecting `protected` set, eliminating repeated full dictionary scans and `min()` calls.

- [x] **Step 3: Run session store tests**
Run: `python -m unittest run_tests.SessionStoreTests`
Expected: PASS

---

### Task 6: Optimize SQLite Reads in `conversation_reader.py`

**Files:**
- Modify: `conversation_reader.py:49-105`
- Test: `run_tests.py` (ConversationDatabaseReaderTests)

- [x] **Step 1: Connect in read-only URI mode (`file:{path}?mode=ro`)**
Prevent write-lock contention with background workers writing to the database.

- [x] **Step 2: Query targeted step rows with specific SQL filter**
Fetch `SELECT step_payload FROM steps WHERE step_type = 15 AND status = 3 ORDER BY idx DESC` first to avoid loading unnecessary tool outputs and user payloads into memory.

- [x] **Step 3: Run conversation reader tests**
Run: `python -m unittest run_tests.ConversationDatabaseReaderTests`
Expected: PASS

---

### Task 7: Full Regression Suite Verification

- [x] **Step 1: Run complete test suite**
Run: `python run_tests.py -v`
Expected: 45 tests pass, 0 failures, 1 skipped.
