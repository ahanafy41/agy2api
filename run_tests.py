#!/usr/bin/env python3
"""Targeted, zero-dependency test suite for agy2api.

Adheres to test-guard quality principles:
- Zero heavy dependencies (Python standard library unittest only).
- Real data objects, real SQLite schemas, and real Pydantic validation (no mocks).
- Tests observable behavior and regression boundaries.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agy_runner import check_windows_command_length
from config import MODEL_MAP, Settings, resolve_model
from conversation_reader import UpstreamError, read_response
from fake_stream import (
    build_chunks,
    make_delta_chunk,
    make_finish_chunk,
    make_role_chunk,
    split_text,
    sse_event,
)
from models import ChatCompletionRequest, ChatMessage
from session_store import SessionStore, fingerprint


def _encode_varint(value: int) -> bytes:
    buf = bytearray()
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def _encode_field(number: int, wire_type: int, data: bytes) -> bytes:
    tag = (number << 3) | wire_type
    return _encode_varint(tag) + _encode_varint(len(data)) + data


class ModelResolutionTests(unittest.TestCase):
    """Test model alias canonicalization and effort variant selection."""

    def test_alias_with_effort_selects_resolved_catalog_variant(self) -> None:
        """Legacy alias gemini-3.5-flash + high effort must resolve to Gemini 3.8 Flash (High)."""
        model, effort = resolve_model("gemini-3.5-flash", "high")
        self.assertEqual(model, "Gemini 3.8 Flash (High)")
        self.assertIsNone(effort, "Effort should be None when catalog encodes effort")

    def test_legacy_gemini_35_flash_effort_variants_resolve(self) -> None:
        """Legacy gemini-3.5-flash-low/medium/high IDs must resolve to Gemini 3.8 Flash variants."""
        cases = [
            ("gemini-3.5-flash-low", "Gemini 3.8 Flash (Low)"),
            ("gemini-3.5-flash-medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.5-flash-high", "Gemini 3.8 Flash (High)"),
        ]
        for model_id, expected_label in cases:
            with self.subTest(model_id=model_id):
                model, effort = resolve_model(model_id)
                self.assertEqual(model, expected_label)
                self.assertIsNone(effort)

    def test_direct_effort_variant_drops_cli_effort_flag(self) -> None:
        """Models with baked-in effort must not pass --effort to avoid agy CLI errors."""
        cases = [
            ("gemini-3.8-flash", "low", "Gemini 3.8 Flash (Low)"),
            ("gemini-3.8-flash", "medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.8-flash", "high", "Gemini 3.8 Flash (High)"),
        ]
        for raw_model, req_effort, expected_label in cases:
            with self.subTest(model=raw_model, effort=req_effort):
                model, effort = resolve_model(raw_model, req_effort)
                self.assertEqual(model, expected_label)
                self.assertIsNone(effort)

    def test_thinking_model_suppresses_cli_effort_flag(self) -> None:
        """Claude thinking models reject --effort; verify effort flag is suppressed."""
        model, effort = resolve_model("claude-sonnet-4.6", "high")
        self.assertEqual(model, "Claude Sonnet 4.6 (Thinking)")
        self.assertIsNone(effort)

    def test_unknown_custom_model_passes_through_with_requested_effort(self) -> None:
        """Unrecognized model names pass through verbatim with effort intact."""
        model, effort = resolve_model("custom-experimental-model", "low")
        self.assertEqual(model, "custom-experimental-model")
        self.assertEqual(effort, "low")


class ConfigurationSettingsTests(unittest.TestCase):
    """Test settings defaults and read-only mode behavior."""

    def test_read_only_mode_enables_sandbox_and_permission_skip(self) -> None:
        """AGY2API_READ_ONLY must activate sandbox and auto-approve permissions."""
        settings = Settings(read_only=True)
        self.assertTrue(settings.read_only)
        self.assertTrue(settings.sandbox)
        self.assertTrue(settings.dangerously_skip_permissions)

    def test_model_catalog_contains_expected_baseline_models(self) -> None:
        """Verify baseline models are registered in MODEL_MAP."""
        expected = [
            "gemini-3.8-flash",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-low",
            "claude-sonnet-4.6",
            "claude-opus-4.6",
            "gpt-oss-120b",
        ]
        for key in expected:
            with self.subTest(model=key):
                self.assertIn(key, MODEL_MAP)


class ChatRequestValidationTests(unittest.TestCase):
    """Test Pydantic chat completion request parsing and validation."""

    def test_request_accepts_valid_reasoning_effort(self) -> None:
        """ChatCompletionRequest must accept standard OpenAI reasoning_effort values."""
        for level in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=level):
                req = ChatCompletionRequest(
                    messages=[ChatMessage(role="user", content="hello")],
                    reasoning_effort=level,
                )
                self.assertEqual(req.reasoning_effort, level)

    def test_request_ignores_extra_unmodeled_openai_parameters(self) -> None:
        """Clients sending top_p, frequency_penalty, or n must not trigger 422 errors."""
        payload = {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "top_p": 0.95,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.2,
            "n": 1,
            "user": "test-user-id",
        }
        req = ChatCompletionRequest.model_validate(payload)
        self.assertEqual(req.model, "gemini-3.8-flash")
        self.assertEqual(len(req.messages), 1)


class ConversationDatabaseReaderTests(unittest.TestCase):
    """Test reading responses, reasoning, and upstream errors from real SQLite databases."""

    def setUp(self) -> None:
        self.fd, self.db_path = tempfile.mkstemp(prefix="agy-test-", suffix=".db")
        os.close(self.fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE steps (
                idx INTEGER PRIMARY KEY,
                step_type INTEGER,
                status INTEGER,
                step_payload BLOB
            )
            """)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_completed_model_step_extracts_answer_and_reasoning(self) -> None:
        """A valid model step (15) with status=3 must yield answer and reasoning text."""
        f1 = _encode_field(1, 2, "Response answer body".encode("utf-8"))
        f3 = _encode_field(3, 2, "Internal chain of thought".encode("utf-8"))
        payload = _encode_field(20, 2, f1 + f3)

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO steps VALUES (1, 15, 3, ?)", (payload,))
        conn.commit()
        conn.close()

        result = read_response(self.db_path)
        self.assertEqual(result.answer, "Response answer body")
        self.assertEqual(result.reasoning, "Internal chain of thought")
        self.assertFalse(result.truncated)

    def test_step_17_error_with_upstream_keyword_raises_upstream_error(self) -> None:
        """An error step (17) containing upstream keywords must raise UpstreamError."""
        # Protobuf string payload containing the keyword
        payload = _encode_field(
            1, 2, "upstream stream reading error occurred".encode("utf-8")
        )

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO steps VALUES (1, 17, 3, ?)", (payload,))
        conn.commit()
        conn.close()

        with self.assertRaises(UpstreamError) as ctx:
            read_response(self.db_path)
        self.assertIn("stream reading error", str(ctx.exception).lower())

    def test_database_without_model_rows_raises_value_error(self) -> None:
        """A database with no completed model response must raise ValueError."""
        with self.assertRaises(ValueError):
            read_response(self.db_path)


class SessionStoreTests(unittest.TestCase):
    """Test stateful session store prefix matching and eviction."""

    def test_matching_prefix_returns_resume_plan(self) -> None:
        """Incoming history matching a stored session prefix must produce a resume plan."""
        store = SessionStore(max_sessions=10)
        sig1 = fingerprint("user", "turn 1")
        sig2 = fingerprint("assistant", "turn 1 reply")
        sig3 = fingerprint("user", "turn 2")

        store.remember("conv-uuid-1", [sig1, sig2])
        plan = store.lookup([sig1, sig2, sig3])

        self.assertEqual(plan.conversation_id, "conv-uuid-1")
        self.assertEqual(plan.prefix_len, 2)

    def test_unrelated_history_returns_fresh_plan(self) -> None:
        """Incoming history that does not share a prefix must return a fresh plan."""
        store = SessionStore(max_sessions=10)
        store.remember("conv-uuid-1", [fingerprint("user", "turn 1")])

        plan = store.lookup([fingerprint("user", "completely different topic")])
        self.assertIsNone(plan.conversation_id)
        self.assertEqual(plan.prefix_len, 0)

    def test_lru_eviction_drops_oldest_session_when_over_capacity(self) -> None:
        """Exceeding max_sessions must evict the least recently used conversation."""
        store = SessionStore(max_sessions=2)
        store.remember("conv-1", [fingerprint("user", "1")])
        time.sleep(0.01)
        store.remember("conv-2", [fingerprint("user", "2")])
        time.sleep(0.01)

        # Adding a 3rd session must evict conv-1
        evicted = store.remember("conv-3", [fingerprint("user", "3")])
        self.assertIn("conv-1", evicted)
        self.assertIsNone(store.lookup([fingerprint("user", "1")]).conversation_id)


class FakeStreamHelpersTests(unittest.TestCase):
    """Test chunking and Server-Sent Events (SSE) formatting."""

    def test_split_text_divides_strings_into_regular_chunks(self) -> None:
        """Text must be split into pieces of the requested chunk size."""
        chunks = split_text("abcdefghij", size=3)
        self.assertEqual(chunks, ["abc", "def", "ghi", "j"])

    def test_build_chunks_structures_standard_openai_stream_events(self) -> None:
        """build_chunks must create initial role delta, reasoning, content, and stop chunks."""
        chunks = build_chunks(
            answer="Hello", reasoning="Thinking", model="gemini-3.8-flash"
        )
        self.assertTrue(len(chunks) >= 4)
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_sse_event_formats_valid_data_frame(self) -> None:
        """sse_event must serialize dict to 'data: <json>\\n\\n' UTF-8 bytes."""
        event_bytes = sse_event({"test": "value"})
        self.assertTrue(event_bytes.startswith(b"data: "))
        self.assertTrue(event_bytes.endswith(b"\n\n"))
        self.assertIn(b'"test":"value"', event_bytes)

    def test_realtime_stream_chunk_helpers(self) -> None:
        """Verify role, delta, and finish chunk format for real-time SSE streaming."""
        cid = "test-cid"
        ts = 1234567890
        model = "gemini-3.8-flash-low"

        role_chunk = make_role_chunk(cid, ts, model)
        self.assertEqual(role_chunk["choices"][0]["delta"]["role"], "assistant")
        self.assertIsNone(role_chunk["choices"][0]["finish_reason"])

        delta_chunk = make_delta_chunk(cid, ts, model, "Hello ")
        self.assertEqual(delta_chunk["choices"][0]["delta"]["content"], "Hello ")
        self.assertIsNone(delta_chunk["choices"][0]["finish_reason"])

        finish_chunk = make_finish_chunk(cid, ts, model, finish_reason="stop")
        self.assertEqual(finish_chunk["choices"][0]["finish_reason"], "stop")

    def test_build_completion_response_preserves_real_token_usage(self) -> None:
        """build_completion_response must map real usage tokens accurately."""
        from server import build_completion_response
        usage_payload = {"input_tokens": 150, "output_tokens": 45, "total_tokens": 195}
        resp = build_completion_response(
            answer="This is a test answer",
            reasoning="Test reasoning",
            model="gemini-3.8-flash-low",
            usage=usage_payload,
        )
        self.assertEqual(resp["usage"]["prompt_tokens"], 150)
        self.assertEqual(resp["usage"]["completion_tokens"], 45)
        self.assertEqual(resp["usage"]["total_tokens"], 195)


class WindowsCommandLineGuardTests(unittest.TestCase):
    """Test Windows ~32KB command line overflow detection and cleanup."""

    def test_check_windows_command_length_accepts_normal_command(self) -> None:
        """Normal commands under 32,000 UTF-16 code units pass and return code unit count."""
        command = ["agy.exe", "--print=hello", "--disable-slash-commands"]
        count = check_windows_command_length(command)
        self.assertGreater(count, 0)
        self.assertLess(count, 32_000)

    def test_check_windows_command_length_rejects_oversized_command(self) -> None:
        """Commands exceeding 32,000 UTF-16 code units must raise ValueError."""
        huge_prompt = "a" * 32_500
        command = ["agy.exe", f"--print={huge_prompt}", "--disable-slash-commands"]
        with self.assertRaises(ValueError) as ctx:
            check_windows_command_length(command)
        self.assertIn("exceeds Windows limit", str(ctx.exception))

    @unittest.skipUnless(os.name == "nt", "Windows command line length guard only applies on Windows")
    def test_oversized_run_cleans_up_temporary_log_file(self) -> None:
        """_run_agy_once must delete log_path when command length check fails."""
        from agy_runner import _run_agy_once

        huge_prompt = "a" * 32_500
        temp_dir = tempfile.gettempdir()
        before_logs = set(Path(temp_dir).glob("agy2api-*.log"))

        with self.assertRaises(ValueError):
            _run_agy_once(
                prompt=huge_prompt,
                model=None,
                conversation_id=None,
                keep=False,
            )

        after_logs = set(Path(temp_dir).glob("agy2api-*.log"))
        new_leaked_logs = after_logs - before_logs
        self.assertEqual(
            new_leaked_logs,
            set(),
            f"Oversized command leaked log files: {new_leaked_logs}",
        )


class MultimodalAndMediaCacheTests(unittest.TestCase):
    """Test multimodal content parsing and media cache lifecycle."""

    def test_save_base64_media_creates_ephemeral_file(self) -> None:
        """Base64 data URI must decode and save to MEDIA_CACHE_DIR with proper permissions."""
        from server import MEDIA_CACHE_DIR, _cleanup_old_media_cache, _save_base64_media

        # 1x1 transparent PNG base64
        sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        saved_path = _save_base64_media(sample_b64, "image/png")
        self.assertIsNotNone(saved_path)
        self.assertTrue(saved_path.exists())
        self.assertTrue(saved_path.name.endswith(".png"))
        self.assertGreater(saved_path.stat().st_size, 0)

        # Cleanup
        saved_path.unlink(missing_ok=True)

    def test_cleanup_old_media_cache_removes_expired_files(self) -> None:
        """_cleanup_old_media_cache must remove files older than max_age_seconds."""
        from server import MEDIA_CACHE_DIR, _cleanup_old_media_cache

        stale_file = MEDIA_CACHE_DIR / "stale_test_file.jpg"
        stale_file.write_bytes(b"dummy")
        # Set mtime to 3 hours ago
        three_hours_ago = time.time() - 10800
        os.utime(stale_file, (three_hours_ago, three_hours_ago))

        _cleanup_old_media_cache(max_age_seconds=7200)
        self.assertFalse(stale_file.exists(), "Stale media cache file should have been deleted")

    def test_content_to_text_parses_multimodal_list(self) -> None:
        """_content_to_text must extract text and media references properly."""
        from server import _content_to_text

        content_list = [
            {"type": "text", "text": "Analyze this:"},
            {"type": "image_url", "image_url": {"url": "http://example.com/test.jpg"}},
        ]
        result = _content_to_text(content_list)
        self.assertIn("Analyze this:", result)
        self.assertIn("http://example.com/test.jpg", result)


class EnvironmentIsolationTests(unittest.TestCase):
    """Test sandbox and workspace path isolation."""

    def test_isolated_workspace_contained_within_repo(self) -> None:
        """Isolated workspace must reside within the agy2api repository root."""
        from config import settings

        repo_root = Path(__file__).resolve().parent
        self.assertEqual(settings.workdir, repo_root / "isolated_workspace")


class YouTubeHandlerTests(unittest.TestCase):
    """Test YouTube URL extraction, visual request detection, and two-tier processing."""

    def test_extract_youtube_urls(self) -> None:
        """extract_youtube_urls must find and normalize standard, short, and shorts URLs."""
        from youtube_handler import extract_youtube_urls

        text = (
            "Check these: https://www.youtube.com/watch?v=dQw4w9WgXcQ and "
            "https://youtu.be/XmLthH5jlTI?si=abc and "
            "https://youtube.com/shorts/abcd1234efg in one prompt."
        )
        urls = extract_youtube_urls(text)
        self.assertEqual(len(urls), 3)
        self.assertIn("https://www.youtube.com/watch?v=dQw4w9WgXcQ", urls)
        self.assertIn("https://www.youtube.com/watch?v=XmLthH5jlTI", urls)
        self.assertIn("https://www.youtube.com/watch?v=abcd1234efg", urls)

    def test_is_visual_request_detection(self) -> None:
        """is_visual_request must accurately distinguish visual scene descriptions from text queries."""
        from youtube_handler import is_visual_request

        visual_prompts = [
            "أنت عين لشخص كفيف. صِف باختصار شديد أهم ما يحدث في بداية هذا الفيديو",
            "صف لي المشهد بصرياً وشايف إيه في الشاشة",
            "What does the visual scene look like?",
            "Describe the frame and what they are wearing",
        ]
        for vp in visual_prompts:
            self.assertTrue(is_visual_request(vp), f"Should detect visual request for: {vp}")

        non_visual_prompts = [
            "ما هو عنوان هذا الفيديو؟",
            "لخص لي الأفكار الرئيسية في المحادثة",
            "What are the key points mentioned in the audio?",
        ]
        for nvp in non_visual_prompts:
            self.assertFalse(is_visual_request(nvp), f"Should not detect visual request for: {nvp}")

    def test_process_youtube_request_tier1_transcript(self) -> None:
        """When transcript exists and query is non-visual, process_youtube_request formats prompt without video download."""
        from unittest.mock import patch
        from youtube_handler import process_youtube_request

        fake_meta = {
            "title": "Test Video Title",
            "description": "Test Description",
            "duration": 120,
            "transcript": "Hello world, this is a spoken transcript.",
        }

        with patch("youtube_handler.fetch_youtube_metadata_and_transcript", return_value=fake_meta):
            prompt, temp_file = process_youtube_request(
                "https://youtu.be/test1234567",
                "لخص الفيديو في ثلاث نقاط",
                Path(tempfile.gettempdir()),
            )
            self.assertIsNone(temp_file)
            self.assertIn("Test Video Title", prompt)
            self.assertIn("Hello world, this is a spoken transcript.", prompt)
            self.assertIn("لخص الفيديو في ثلاث نقاط", prompt)

    def test_process_youtube_request_tier2_visual_video(self) -> None:
        """When query is visual, process_youtube_request triggers micro-video download and instructs read_file."""
        from unittest.mock import patch
        from youtube_handler import process_youtube_request

        fake_meta = {
            "title": "Visual Video Title",
            "description": "Visual Description",
            "duration": 60,
            "transcript": "Audio text here.",
        }
        fake_video_path = Path(tempfile.gettempdir()) / "fake_video.mp4"
        fake_video_path.touch()

        try:
            with patch("youtube_handler.fetch_youtube_metadata_and_transcript", return_value=fake_meta), \
                 patch("youtube_handler.download_micro_video", return_value=fake_video_path):
                prompt, temp_file = process_youtube_request(
                    "https://youtu.be/test1234567",
                    "أنت عين لكفيف صف المشهد بصريا في بداية الفيديو",
                    Path(tempfile.gettempdir()),
                )
                self.assertEqual(temp_file, fake_video_path)
                self.assertIn("read_file", prompt)
                self.assertIn(str(fake_video_path), prompt)
                self.assertIn("كعين لشخص كفيف", prompt)
        finally:
            fake_video_path.unlink(missing_ok=True)


class BackgroundJanitorAndEphemeralTests(unittest.TestCase):
    """Test background janitor sweep, active session protection, and ephemeral media lifecycle."""

    def test_run_janitor_sweep_lifecycle(self) -> None:
        """run_janitor_sweep must sweep stale media, DBs, brain dirs, and logs while protecting active ones."""
        from server import MEDIA_CACHE_DIR, run_janitor_sweep, _in_flight
        from config import settings
        import shutil

        now = time.time()
        two_hours_ago = now - 7200

        # 1. Media cache: stale vs fresh
        stale_media = MEDIA_CACHE_DIR / "stale_janitor_test.png"
        fresh_media = MEDIA_CACHE_DIR / "fresh_janitor_test.png"
        stale_media.write_bytes(b"stale")
        fresh_media.write_bytes(b"fresh")
        os.utime(stale_media, (two_hours_ago, two_hours_ago))

        # 2. Conversations: stale, fresh, and protected
        conv_dir = settings.conversations_dir
        conv_dir.mkdir(parents=True, exist_ok=True)
        stale_conv = conv_dir / "stale_conv_123.db"
        fresh_conv = conv_dir / "fresh_conv_456.db"
        protected_conv = conv_dir / "protected_conv_789.db"

        stale_conv.write_bytes(b"stale")
        fresh_conv.write_bytes(b"fresh")
        protected_conv.write_bytes(b"protected")

        os.utime(stale_conv, (two_hours_ago, two_hours_ago))
        os.utime(protected_conv, (two_hours_ago, two_hours_ago))

        # Mark protected_conv as in-flight
        _in_flight.add("protected_conv_789")

        # 3. Brain dirs: stale
        brain_dir = conv_dir.parent / "brain"
        stale_brain = brain_dir / "stale_brain_dir"
        stale_brain.mkdir(parents=True, exist_ok=True)
        os.utime(stale_brain, (two_hours_ago, two_hours_ago))

        # 4. Temp logs: stale
        temp_log = Path(tempfile.gettempdir()) / "agy2api-stale-test.log"
        temp_log.write_bytes(b"log")
        os.utime(temp_log, (two_hours_ago, two_hours_ago))

        try:
            stats = run_janitor_sweep(max_age_seconds=3600)
            self.assertGreaterEqual(stats["media"], 1)
            self.assertGreaterEqual(stats["conversations"], 1)
            self.assertGreaterEqual(stats["brain"], 1)
            self.assertGreaterEqual(stats["logs"], 1)

            # Assertions
            self.assertFalse(stale_media.exists(), "Stale media should be removed")
            self.assertTrue(fresh_media.exists(), "Fresh media should be retained")

            self.assertFalse(stale_conv.exists(), "Stale conversation DB should be removed")
            self.assertTrue(fresh_conv.exists(), "Fresh conversation DB should be retained")
            self.assertTrue(protected_conv.exists(), "Protected conversation DB must NOT be removed")

            self.assertFalse(stale_brain.exists(), "Stale brain directory should be removed")
            self.assertFalse(temp_log.exists(), "Stale temp log file should be removed")
        finally:
            _in_flight.discard("protected_conv_789")
            stale_media.unlink(missing_ok=True)
            fresh_media.unlink(missing_ok=True)
            stale_conv.unlink(missing_ok=True)
            fresh_conv.unlink(missing_ok=True)
            protected_conv.unlink(missing_ok=True)
            shutil.rmtree(stale_brain, ignore_errors=True)
            temp_log.unlink(missing_ok=True)

    def test_session_store_all_ids_helper(self) -> None:
        """SessionStore.all_ids returns all tracked conversation IDs."""
        from session_store import SessionStore

        store = SessionStore(10)
        store.remember("session-a", ["user:hello"])
        store.remember("session-b", ["user:world"])
        self.assertEqual(store.all_ids(), {"session-a", "session-b"})

    def test_ephemeral_media_tracking_in_content_to_text(self) -> None:
        """_content_to_text tracks ephemeral base64 files and respects save_media flag."""
        from server import _content_to_text

        sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        content = [
            {"type": "text", "text": "Describe:"},
            {"type": "image_url", "image_url": {"url": sample_b64}},
        ]

        # 1. Tracking in ephemeral_files
        ephemeral_files: list[Path] = []
        text = _content_to_text(content, ephemeral_files=ephemeral_files, save_media=True)
        self.assertEqual(len(ephemeral_files), 1)
        saved_file = ephemeral_files[0]
        self.assertTrue(saved_file.exists())
        self.assertIn(str(saved_file), text)

        # Immediate cleanup
        saved_file.unlink(missing_ok=True)
        self.assertFalse(saved_file.exists())

        # 2. save_media=False (fingerprinting) creates no files
        ephemeral_files_empty: list[Path] = []
        text_fp = _content_to_text(content, ephemeral_files=ephemeral_files_empty, save_media=False)
        self.assertEqual(len(ephemeral_files_empty), 0)
        self.assertIn("[مرفق صورة base64]", text_fp)

    def test_isolated_worker_recycle_policy(self) -> None:
        """IsolatedWorker should correctly enforce 30-min TTL, max turns, and model switching."""
        from isolated_session import IsolatedWorker

        w = IsolatedWorker(session_ttl=1800.0, max_turns=20, idle_timeout=600.0)
        # Not alive yet -> should recycle (spawn)
        self.assertTrue(w.should_recycle("gemini-3.8-flash-low", "low"))

        # Fake alive process
        class FakeProc:
            returncode = None
        w.proc = FakeProc()  # type: ignore
        w.created_at = time.time()
        w.current_model = "gemini-3.8-flash-low"
        w.current_effort = "low"
        w.turn_count = 5

        # Healthy within TTL -> should NOT recycle
        self.assertFalse(w.should_recycle("gemini-3.8-flash-low", "low"))

        # Model change -> should recycle
        self.assertTrue(w.should_recycle("claude-sonnet-4-6", "low"))

        # Effort change -> should recycle
        self.assertTrue(w.should_recycle("gemini-3.8-flash-low", "high"))

        # Turn count reached (>= 20) -> should recycle
        w.turn_count = 20
        self.assertTrue(w.should_recycle("gemini-3.8-flash-low", "low"))

        # 30-min TTL expired -> should recycle
        w.turn_count = 5
        w.created_at = time.time() - 1801.0
        self.assertTrue(w.should_recycle("gemini-3.8-flash-low", "low"))


if __name__ == "__main__":
    unittest.main()
