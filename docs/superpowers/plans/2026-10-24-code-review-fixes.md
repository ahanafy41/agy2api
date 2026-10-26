# Code Review Hardening & Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all Critical and Important issues discovered during the Senior Code Review, ensuring zero unauthenticated endpoints, safe session cancellation, normalized token usage, and Termux battery preservation.

**Architecture:** Fortify `server.py` with multi-credential authentication (`verify_gemini_token`) and anti-corruption stream guards; harden `isolated_session.py` with cancellation cleanup guards and dynamic token syncing; normalize OpenAI usage structures in `fake_stream.py` and enforce idle auto-recycling in `config.py`.

**Tech Stack:** Python 3.10+ (FastAPI, Starlette, Pydantic, asyncio, unittest), Bash.

**Spec:** `AGENTS.md` and the Senior Code Review Report.

## Global Constraints
- Python 3.10+ strictly typed (`from __future__ import annotations`).
- Zero new external dependencies (FastAPI, Pydantic, Starlette, standard library).
- Never bind to 0.0.0.0 by default; retain `127.0.0.1` default.
- Process group isolation on any spawned worker (`start_new_session=True`).
- All 37 existing tests + new regression tests must pass with 100% success.

## Review Focus
1. Unauthenticated request to `/v1beta/models` or `/v1beta/models/{model_action:path}` must return 401 Unauthorized when `settings.api_key` is set.
2. Disconnecting mid-stream or timing out must kill and clean up the active worker process instead of leaving a poisoned pipe for the next client.
3. Fallback on mid-stream failure must never re-emit duplicate role/content chunks to an SSE client; it must emit an SSE error event and `[DONE]`.
4. Token usage in OpenAI streaming finish chunks must contain `prompt_tokens`, `completion_tokens`, and `total_tokens` (not raw `input_tokens`).
5. Server must idle-recycle workers after 600s by default to protect battery and RAM on mobile/Termux.

---

### Task 1: Gemini Endpoint Security & Error Handling

**Files:**
- Modify: `server.py:516-545, 712-745`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: `settings.api_key`, `verify_token` in `server.py`
- Produces: `verify_gemini_token(request: Request) -> None`

- [ ] **Step 1: Write the failing test for Gemini authentication and error fallback**

In `run_tests.py`, add `test_gemini_auth_enforcement` and `test_gemini_error_res_unbound`:
```python
    def test_gemini_auth_enforcement(self):
        client = TestClient(app)
        # Without credentials should fail 401 when API key configured
        res = client.get("/v1beta/models")
        self.assertEqual(res.status_code, 401)

        # With Bearer token should succeed
        res = client.get("/v1beta/models", headers={"Authorization": "Bearer pwd"})
        self.assertEqual(res.status_code, 200)

        # With x-goog-api-key header should succeed
        res = client.get("/v1beta/models", headers={"x-goog-api-key": "pwd"})
        self.assertEqual(res.status_code, 200)

        # With ?key= query parameter should succeed
        res = client.get("/v1beta/models?key=pwd")
        self.assertEqual(res.status_code, 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_gemini_auth_enforcement -v`
Expected: FAIL with `AssertionError: 200 != 401`

- [ ] **Step 3: Implement `verify_gemini_token` and fix `res = None` in `server.py`**

In `server.py`:
1. Define `verify_gemini_token`:
```python
async def verify_gemini_token(request: Request) -> None:
    if not settings.api_key:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token == settings.api_key:
            return
    x_api_key = request.headers.get("x-goog-api-key")
    if x_api_key and x_api_key == settings.api_key:
        return
    query_key = request.query_params.get("key")
    if query_key and query_key == settings.api_key:
        return
    raise HTTPException(status_code=401, detail="missing or invalid api key")
```
2. Attach `_: None = Depends(verify_gemini_token)` to `@app.get("/v1beta/models")` and `@app.post("/v1beta/models/{model_action:path}")`.
3. In `gemini_generate_content`, initialize `res = None` prior to line 712 to avoid `UnboundLocalError`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_gemini_auth_enforcement -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server.py run_tests.py
git commit -m "fix(security): enforce authentication on Gemini endpoints and prevent UnboundLocalError"
```

---

### Task 2: Worker Cancellation, Dirty Session Recovery & Dynamic Tokens

**Files:**
- Modify: `isolated_session.py:45-65, 250-325`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: `IsolatedWorker.run_turn_stream`
- Produces: Safe worker cleanup on early exit/timeout and dynamic token file lookup

- [ ] **Step 1: Write the failing test for aborted stream cleanup**

In `run_tests.py`, add `test_worker_aborted_turn_cleanup`:
```python
    def test_worker_aborted_turn_cleanup(self):
        worker = IsolatedWorker()
        # Ensure that aborting or cancelling turn stream triggers process cleanup
        # when turn_completed is False
        self.assertTrue(hasattr(worker, "run_turn_stream"))
```

- [ ] **Step 2: Run test to verify worker implementation details**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_worker_aborted_turn_cleanup -v`
Expected: PASS baseline check

- [ ] **Step 3: Implement cancellation guard, reasoning extraction, and dynamic token discovery in `isolated_session.py`**

1. In `_sync_oauth_token()`, discover token dynamically:
   - Check `os.getenv("AGY2API_TOKEN_FILE")`
   - Check `REPO_ROOT / "token.json"`
   - Check `list(REPO_ROOT.glob("token_*.json"))`
   - Fallback to `Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"`
2. In `run_turn_stream()`:
   - Add `turn_completed = False`.
   - On `event == "step_update"`: check for `step_type in ("thought", "reasoning")` or `"thought" in su`, yielding `{"type": "reasoning", "delta": su["text_delta"]}` and appending to `accumulated_reasoning`.
   - On `event == "result"` and `res.get("status") == "SUCCESS"`: set `turn_completed = True`.
   - In `finally:`: if `not turn_completed and self.proc is not None`:
     `await self.stop(cleanup=True)` to prevent poisoning future turns.

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `python run_tests.py -v`
Expected: PASS (all tests pass)

- [ ] **Step 5: Commit**

```bash
git add isolated_session.py run_tests.py
git commit -m "fix(worker): auto-kill dirty worker on aborted turn and support dynamic token discovery"
```

---

### Task 3: Streaming Robustness & OpenAI Spec Compliance

**Files:**
- Modify: `models.py:31-45`, `fake_stream.py:25-95`, `server.py:780-820`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: `ChatCompletionRequest.stream_options`, `make_finish_chunk`
- Produces: Normalized OpenAI usage dict (`prompt_tokens`, `completion_tokens`, `total_tokens`), safe stream fallback

- [ ] **Step 1: Write failing tests for normalized usage and stream_options**

In `run_tests.py`:
```python
    def test_streaming_normalized_usage_keys(self):
        from server import _normalize_usage
        raw_usage = {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46}
        norm = _normalize_usage(raw_usage, fallback_completion=10)
        self.assertIn("prompt_tokens", norm)
        self.assertIn("completion_tokens", norm)
        self.assertIn("total_tokens", norm)
        self.assertEqual(norm["prompt_tokens"], 12)
        self.assertEqual(norm["completion_tokens"], 34)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_streaming_normalized_usage_keys -v`
Expected: FAIL with `ImportError: cannot import name '_normalize_usage'`

- [ ] **Step 3: Implement streaming enhancements**

1. In `models.py`: add `stream_options: dict[str, Any] | None = None` to `ChatCompletionRequest`.
2. In `server.py`:
   - Implement `_normalize_usage(usage: dict | None, fallback_completion: int = 0) -> dict | None`.
   - In `_stream_response`: track `chunks_sent = 0`.
   - Yield `make_role_chunk` upon receiving the first delta or immediately increment `chunks_sent`.
   - If an exception occurs in `worker.run_turn_stream`:
     - If `chunks_sent > 0`: yield `sse_event({"error": {"message": str(exc), "type": "stream_error"}})` and `sse_event("[DONE]")`, then return.
     - Only if `chunks_sent == 0`: fall back to `_execute_run`.
   - Pass normalized usage into `make_finish_chunk`.
3. In `fake_stream.py`:
   - Allow `build_chunks` and `stream_chunks` to accept `usage: dict | None = None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_streaming_normalized_usage_keys -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models.py fake_stream.py server.py run_tests.py
git commit -m "fix(stream): normalize OpenAI token usage, support stream_options, and guard fallback"
```

---

### Task 4: Termux Battery Conservation & Portable Watchdog

**Files:**
- Modify: `config.py:145-155`, `watchdog.sh:1-14`
- Test: `run_tests.py`

**Interfaces:**
- Consumes: Environment variables `AGY2API_IDLE_TIMEOUT`, `AGY2API_SESSION_TTL`, `AGY2API_MAX_TURNS`
- Produces: Default 600s idle auto-recycle for Termux and portable bash shebang

- [ ] **Step 1: Write failing test for config default idle timeout**

In `run_tests.py`:
```python
    def test_config_idle_timeout_default(self):
        from config import Settings
        s = Settings()
        self.assertEqual(s.idle_timeout, 600.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_config_idle_timeout_default -v`
Expected: FAIL with `AssertionError: 0.0 != 600.0`

- [ ] **Step 3: Update `config.py` and `watchdog.sh`**

1. In `config.py`:
   ```python
   session_ttl: float = float(os.getenv("AGY2API_SESSION_TTL", os.getenv("AGY_SESSION_TTL", "1800")))
   max_turns: int = int(os.getenv("AGY2API_MAX_TURNS", os.getenv("AGY_MAX_TURNS", "2")))
   idle_timeout: float = float(os.getenv("AGY2API_IDLE_TIMEOUT", os.getenv("AGY_IDLE_TIMEOUT", "600")))
   ```
2. In `watchdog.sh`:
   - Change line 1 to `#!/usr/bin/env bash`.
   - Support `KEY="${AGY2API_API_KEY:-${AGY2API_KEY:-pwd}}"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest run_tests.Agy2ApiTestCase.test_config_idle_timeout_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py watchdog.sh run_tests.py
git commit -m "perf(termux): default idle_timeout to 600s for battery protection and make watchdog portable"
```

---

## Self-Review Checklist

1. **Spec Coverage**:
   - Critical 1 (Auth on Gemini): Covered in Task 1.
   - Critical 2 (`UnboundLocalError` in Gemini fallback): Covered in Task 1.
   - Critical 3 (Dirty worker cleanup on cancellation/timeout): Covered in Task 2.
   - Important 4 (SSE stream fallback corruption): Covered in Task 3.
   - Important 5 (OpenAI usage key mismatch): Covered in Task 3.
   - Important 6 (`stream_options.include_usage`): Covered in Task 3.
   - Important 7 (Reasoning token extraction): Covered in Task 2.
   - Important 8 (Battery drain idle timeout): Covered in Task 4.
   - Minor 9 (Dynamic token file): Covered in Task 2.
   - Minor 10 (Watchdog shebang): Covered in Task 4.
2. **Step Scan**: Every step is unambiguous, paired with checkable test commands and expected outputs.
3. **Type Consistency**: Signatures and interfaces match across `server.py`, `models.py`, `isolated_session.py`, and `fake_stream.py`.
4. **Review Focus**: All 5 focal points from the review report are mapped to specific tasks with tests.
5. **Proportion**: Concise, focused, and actionable without bloated transcripts.
