"""FastAPI entry point for agy2api."""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pathlib import Path

from agy_runner import (
    cleanup_conversation,
    run_agy_async,
    sweep_all_conversations,
    sweep_orphan_sidecars,
)
from config import is_loopback_host, resolve_model, settings
from fake_stream import (
    completion_id,
    make_delta_chunk,
    make_finish_chunk,
    make_heartbeat,
    make_role_chunk,
    sse_event,
    stream_chunks,
)
from models import ChatCompletionRequest, ModelInfo, ModelList
from session_store import SessionStore, fingerprint
from isolated_session import worker
from youtube_handler import (
    extract_youtube_urls,
    is_visual_request,
    process_youtube_request,
)


logger = logging.getLogger("agy2api")


def _setup_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[agy2api %(asctime)s %(levelname)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_setup_logging()

MEDIA_CACHE_DIR = Path(__file__).resolve().parent / "media_cache"
MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Limit concurrent agy runs (default 1) to mimic human-paced usage and avoid the
# concurrent conversation-DB race. See config.max_concurrency.
_run_semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))

# Experimental stateful-session layer (AGY2API_STATEFUL).
_session_store = SessionStore(settings.max_sessions) if settings.stateful else None
_conv_locks: dict[str, asyncio.Lock] = {}
# Conversations currently being run (resumed). Eviction must not delete these.
_in_flight: set[str] = set()


def _cleanup_old_media_cache(max_age_seconds: int = 7200) -> None:
    """Removes cached media files older than max_age_seconds (default 2 hours)."""
    try:
        now = time.time()
        for f in MEDIA_CACHE_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
                f.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("media_cache cleanup error: %s", exc)


def run_janitor_sweep(max_age_seconds: int = 3600) -> dict[str, int]:
    """Sweeps stale media cache files, orphaned conversation databases,
    brain directories, and temporary log files older than max_age_seconds.

    Protects in-flight conversations, active warm-worker CIDs, and stateful sessions.
    """
    now = time.time()
    stats = {"media": 0, "conversations": 0, "brain": 0, "logs": 0}

    # 1. Sweep media_cache
    try:
        if MEDIA_CACHE_DIR.exists():
            for f in MEDIA_CACHE_DIR.glob("*"):
                try:
                    if f.is_file() and (now - f.stat().st_mtime) >= max_age_seconds:
                        f.unlink(missing_ok=True)
                        stats["media"] += 1
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("janitor media sweep error: %s", exc)

    # Gather protected conversation IDs
    protected_cids: set[str] = set(_in_flight)
    if _session_store is not None:
        try:
            protected_cids.update(_session_store.all_ids())
        except Exception:
            pass

    # 2. Sweep conversations (.db, .db-wal, .db-shm)
    conv_dir = settings.conversations_dir
    try:
        if conv_dir.exists():
            for f in conv_dir.glob("*.db*"):
                try:
                    cid = f.name.split(".db")[0]
                    if cid in protected_cids:
                        continue
                    if (now - f.stat().st_mtime) >= max_age_seconds:
                        f.unlink(missing_ok=True)
                        stats["conversations"] += 1
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("janitor conversations sweep error: %s", exc)

    # 3. Sweep brain directories
    try:
        brain_dir = conv_dir.parent / "brain"
        if brain_dir.is_dir():
            for sub in brain_dir.iterdir():
                try:
                    if not sub.is_dir():
                        continue
                    cid = sub.name
                    if cid in protected_cids:
                        continue
                    if (now - sub.stat().st_mtime) >= max_age_seconds:
                        shutil.rmtree(sub, ignore_errors=True)
                        stats["brain"] += 1
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("janitor brain sweep error: %s", exc)

    # 4. Sweep temporary log files
    temp_dirs = {
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path("/data/data/com.termux/files/usr/tmp"),
    }
    for td in temp_dirs:
        try:
            if not td.is_dir():
                continue
            for log_file in td.glob("agy2api-*.log"):
                try:
                    if (now - log_file.stat().st_mtime) >= max_age_seconds:
                        log_file.unlink(missing_ok=True)
                        stats["logs"] += 1
                except Exception:
                    pass
        except Exception:
            pass

    return stats


async def _background_janitor(interval_seconds: int = 3600, max_age_seconds: int = 3600) -> None:
    """Runs periodic sweeps in the background every interval_seconds."""
    logger.info("Background janitor started (interval=%ds, max_age=%ds)", interval_seconds, max_age_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            stats = run_janitor_sweep(max_age_seconds)
            total = sum(stats.values())
            if total > 0:
                logger.info(
                    "Background janitor swept %d item(s) (media: %d, dbs: %d, brain: %d, logs: %d)",
                    total,
                    stats["media"],
                    stats["conversations"],
                    stats["brain"],
                    stats["logs"],
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Background janitor sweep error: %s", exc)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: run housekeeping sweeps on startup, background janitor periodically,
    and final cleanup on shutdown."""
    # Startup preflight probe from relay.mjs: verify agy readiness and extract version
    try:
        ver_proc = subprocess.run(
            [settings.agy_path, "changelog"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=settings.agy_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if ver_proc.returncode == 0:
            first_line = (ver_proc.stdout.strip().split("\n") or [""])[0]
            logger.info("startup preflight: agy CLI is operational (%s)", first_line)
        else:
            logger.warning("startup preflight: agy changelog returned %d: %s", ver_proc.returncode, ver_proc.stderr.strip())
    except FileNotFoundError:
        logger.error("startup preflight: agy executable not found at %s", settings.agy_path)
    except subprocess.TimeoutExpired:
        logger.warning("startup preflight: agy changelog timed out after 10s")
    except Exception as exc:
        logger.warning("startup preflight failed: %s", exc)

    # Startup housekeeping.
    try:
        if settings.stateful:
            # Hard reset: the in-memory session store starts empty, so any .db
            # files kept alive by a previous run are unreachable orphans. Wipe
            # them now (stateful memory does not survive a restart anyway).
            removed = sweep_all_conversations()
            logger.info("startup (stateful): wiped %d conversation db(s)", removed)
        else:
            removed = sweep_orphan_sidecars()
            if removed:
                logger.info("startup: swept %d orphan SQLite sidecar file(s)", removed)
        startup_stats = run_janitor_sweep(max_age_seconds=settings.janitor_max_age)
        if any(startup_stats.values()):
            logger.info("startup janitor sweep: %s", startup_stats)
    except Exception as exc:  # never block startup on housekeeping
        logger.warning("startup sweep failed: %s", exc)

    janitor_task = asyncio.create_task(
        _background_janitor(interval_seconds=settings.janitor_interval, max_age_seconds=settings.janitor_max_age)
    )
    if settings.fast_session:
        worker.start_sweeper()
        # Pre-warm the isolated worker immediately in background on startup:
        # this completely eliminates the cold-start delay for the first request!
        asyncio.create_task(worker.get_or_spawn("gemini-3.8-flash-low", "low"))

    try:
        yield
    finally:
        janitor_task.cancel()
        if settings.fast_session:
            worker.stop_sweeper()
            await worker.stop(cleanup=True)
        try:
            await janitor_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        # Shutdown housekeeping. The atexit hook covers plain `python server.py`
        # exit too; this fires on uvicorn's graceful stop. Belt-and-suspenders so
        # a clean stop never leaves orphaned stateful sessions behind.
        if settings.stateful:
            try:
                removed = sweep_all_conversations()
                if removed:
                    logger.info("shutdown (stateful): wiped %d conversation db(s)", removed)
            except Exception as exc:  # never block shutdown on housekeeping
                logger.warning("shutdown sweep failed: %s", exc)
        try:
            shutdown_stats = run_janitor_sweep(max_age_seconds=0 if not settings.stateful else settings.janitor_max_age)
            if any(shutdown_stats.values()):
                logger.info("shutdown janitor sweep: %s", shutdown_stats)
        except Exception:
            pass


app = FastAPI(title="agy2api", version="1.1.0", lifespan=_lifespan)
security = HTTPBearer(auto_error=False)


def _stateful_wipe() -> None:
    """Last-resort cleanup of persistent stateful sessions on exit. Covers the
    cases the FastAPI shutdown event misses (atexit-level interpreter shutdown,
    SIGTERM that uvicorn doesn't translate into a graceful shutdown)."""
    if not settings.stateful:
        return
    try:
        removed = sweep_all_conversations()
        if removed:
            logger.info("exit (stateful): wiped %d conversation db(s)", removed)
    except Exception as exc:
        logger.warning("exit wipe failed: %s", exc)


# atexit runs on normal interpreter exit (covers `python server.py` Ctrl-C /
# and sys.exit); the FastAPI shutdown event covers graceful uvicorn stop.
atexit.register(_stateful_wipe)


def _conv_lock(conversation_id: str) -> asyncio.Lock:
    lock = _conv_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _conv_locks[conversation_id] = lock
    return lock


async def _run_agy_guarded(
    prompt: str,
    model: str | None,
    conversation_id: str | None = None,
    keep: bool = False,
    effort: str | None = None,
):
    async with _run_semaphore:
        return await run_agy_async(prompt, model, conversation_id, keep, effort)


async def _execute_run(
    prompt: str,
    model: str | None,
    resume_id: str | None,
    keep: bool,
    effort: str | None = None,
):
    """Run agy, serializing turns of the same resumed conversation so concurrent
    requests for one chat don't corrupt its session.

    Note: planning (_plan_request) happens before this lock, so two *concurrent*
    turns of the SAME chat could plan against stale state. OpenAI clients
    (SillyTavern) serialize turns per chat — they wait for each reply — so this
    is not hit in practice; concurrent turns of one chat are unsupported."""
    if resume_id:
        if settings.fast_session and worker.is_alive() and worker.cid == resume_id:
            try:
                return await worker.run_turn_result(prompt, model=model or "gemini-3.8-flash-low", effort=effort)
            except Exception as exc:
                logger.warning("IsolatedWorker resumed turn failed (%s); falling back to guarded agy: %s", type(exc).__name__, exc)
        async with _conv_lock(resume_id):
            _in_flight.add(resume_id)
            try:
                return await _run_agy_guarded(prompt, model, resume_id, keep, effort)
            finally:
                _in_flight.discard(resume_id)

    if settings.fast_session:
        try:
            return await worker.run_turn_result(prompt, model=model or "gemini-3.8-flash-low", effort=effort)
        except Exception as exc:
            logger.warning("IsolatedWorker turn failed (%s); falling back to one-shot agy: %s", type(exc).__name__, exc)

    return await _run_agy_guarded(prompt, model, None, keep, effort)


def _msg_sigs(messages: list[Any]) -> list[str]:
    return [
        fingerprint(getattr(m, "role", "user"), _content_to_text(getattr(m, "content", ""), save_media=False))
        for m in messages
    ]


def _plan_request(
    messages: list[Any],
    ephemeral_files: list[Path] | None = None,
) -> tuple[str, str | None, list[str]]:
    """Return (prompt_to_send, resume_conversation_id, sigs).

    In stateful mode, if this request continues a known chat, send only the new
    turn against the existing agy conversation. Otherwise send the full history.
    """
    if _session_store is None:
        return format_messages(messages, ephemeral_files=ephemeral_files), None, []
    sigs = _msg_sigs(messages)
    plan = _session_store.lookup(sigs)
    if plan.conversation_id and plan.prefix_len < len(messages):
        new = messages[plan.prefix_len:]
        # Drop leading assistant turns — those are agy's own prior replies that
        # the client echoes back; agy already has them in memory.
        while new and getattr(new[0], "role", "") == "assistant":
            new = new[1:]
        if new:
            return format_messages(new, ephemeral_files=ephemeral_files), plan.conversation_id, sigs
    return format_messages(messages, ephemeral_files=ephemeral_files), None, sigs


def _record_session(conversation_id: str, sigs: list[str]) -> None:
    if _session_store is None or not conversation_id:
        return
    for evicted in _session_store.remember(conversation_id, sigs, protected=_in_flight):
        _conv_locks.pop(evicted, None)  # drop the lock too (no unbounded growth)
        try:
            cleanup_conversation(evicted)
        except Exception as exc:  # housekeeping must not break the request
            logger.warning("evicted-session cleanup failed for %s: %s", evicted, exc)


def _forget_session(resume_id: str | None) -> None:
    """On a failed resumed turn, drop the session so the NEXT turn rebuilds from
    full history instead of resuming a conversation that's now wedged. Without
    this, a broken session keeps failing until the server restarts."""
    if _session_store is not None and resume_id:
        _session_store.forget(resume_id)
        _conv_locks.pop(resume_id, None)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    if not settings.api_key:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="missing bearer token")
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid bearer token")


@app.get("/v1/models", response_model=ModelList)
async def list_models(_: None = Depends(verify_token)) -> ModelList:
    return ModelList(data=[ModelInfo(id=model) for model in settings.models])


@app.post("/v1/chat/completions")
async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
    _: None = Depends(verify_token),
):
    model = request_body.model
    agy_model, agy_effort = resolve_model(model, request_body.reasoning_effort)
    ephemeral_files: list[Path] = []
    prompt, resume_id, sigs = _plan_request(request_body.messages, ephemeral_files=ephemeral_files)
    keep = settings.stateful

    # YouTube URL handling & Ephemeral Cleanup
    yt_urls = extract_youtube_urls(prompt)
    if yt_urls:
        augmented_prompt_parts = [prompt]
        for y_url in yt_urls:
            yt_prompt, temp_file = await asyncio.to_thread(process_youtube_request, y_url, prompt, MEDIA_CACHE_DIR)
            if temp_file:
                ephemeral_files.append(temp_file)
            augmented_prompt_parts.append(yt_prompt)
        prompt = "\n\n".join(augmented_prompt_parts)

    logger.info(
        "request: model=%s (agy=%s) effort=%s (agy_effort=%s) stream=%s messages=%d prompt_chars=%d resume=%s",
        model, agy_model, request_body.reasoning_effort, agy_effort, request_body.stream,
        len(request_body.messages), len(prompt), bool(resume_id),
    )

    if request_body.stream:
        return StreamingResponse(
            _stream_response(prompt, model, agy_model, request, resume_id, keep, sigs, agy_effort, ephemeral_files),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        started = time.time()
        try:
            result = await _execute_run(prompt, agy_model, resume_id, keep, agy_effort)
        except Exception as exc:
            _forget_session(resume_id)
            logger.error(
                "FAILED (non-stream): model=%s prompt_chars=%d after %.1fs -> %s",
                model, len(prompt), time.time() - started, exc, exc_info=True,
            )
            return _error_response(str(exc), status_code=500)

        _record_session(Path(result.db_path).stem, sigs)
        fr = "length" if result.response.truncated else "stop"
        usage_dict = getattr(result.response, "usage", {}) or {}
        logger.info(
            "ok (non-stream): model=%s answer_chars=%d reasoning_chars=%d truncated=%s db=%s resume=%s %.1fs",
            model, len(result.response.answer), len(result.response.reasoning),
            result.response.truncated, result.db_path.name, bool(resume_id), time.time() - started,
        )
        return JSONResponse(
            content=build_completion_response(
                answer=result.response.answer,
                reasoning=result.response.reasoning,
                model=model,
                finish_reason=fr,
                usage=usage_dict,
            )
        )
    finally:
        for ef in set(ephemeral_files):
            try:
                ef.unlink(missing_ok=True)
                logger.info("Cleaned up ephemeral file %s", ef)
            except Exception:
                pass


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1beta/models")
async def gemini_models():
    """Google Gemini API compatibility: List models."""
    models_list = [
        {
            "name": f"models/{m}",
            "version": "001",
            "displayName": m,
            "description": f"Antigravity model {m}",
            "supportedGenerationMethods": ["generateContent"],
        }
        for m in settings.models
    ]
    return JSONResponse(content={"models": models_list})


def _save_base64_media(b64_data: str, mime_type: str = "image/jpeg") -> Path | None:
    """Decodes a base64 media string (raw or data URI) and persists it to media_cache."""
    try:
        _cleanup_old_media_cache()
        if b64_data.startswith("data:"):
            header, _, encoded = b64_data.partition(",")
            if ";" in header:
                mime_part = header.split(";")[0].replace("data:", "").strip()
                if mime_part:
                    mime_type = mime_part
            b64_data = encoded

        ext = ".jpg"
        mime_lower = mime_type.lower()
        if "png" in mime_lower:
            ext = ".png"
        elif "webp" in mime_lower:
            ext = ".webp"
        elif "gif" in mime_lower:
            ext = ".gif"
        elif "pdf" in mime_lower:
            ext = ".pdf"
        elif "mp4" in mime_lower:
            ext = ".mp4"
        elif "mp3" in mime_lower or "mpeg" in mime_lower:
            ext = ".mp3"
        elif "wav" in mime_lower:
            ext = ".wav"
        elif "m4a" in mime_lower:
            ext = ".m4a"

        raw_bytes = base64.b64decode(b64_data)
        fname = f"media_{int(time.time() * 1000)}_{os.urandom(4).hex()}{ext}"
        fpath = MEDIA_CACHE_DIR / fname
        fpath.write_bytes(raw_bytes)
        try:
            os.chmod(fpath, 0o644)
        except OSError:
            pass
        logger.info("Saved media (%s, %d bytes) to %s", mime_type, len(raw_bytes), fpath)
        return fpath
    except Exception as exc:
        logger.error("Failed to decode/save base64 media: %s", exc)
        return None


@app.post("/v1beta/models/{model_action:path}")
async def gemini_generate_content(model_action: str, request: Request):
    """Google Gemini API compatibility: Generate content with full multimodal support."""
    clean_action = model_action.split("?")[0]
    model_id = clean_action.split(":")[0].replace("models/", "")
    resolved_model, agy_effort = resolve_model(model_id)

    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt_parts: list[str] = []
    ephemeral_files: list[Path] = []
    if "system_instruction" in body:
        sys_parts = body["system_instruction"].get("parts", [])
        for sp in sys_parts:
            if "text" in sp:
                prompt_parts.append(f"System: {sp['text']}")

    # Extract overall user text context for YouTube analysis
    user_context = " ".join([
        p["text"]
        for c in body.get("contents", [])
        for p in c.get("parts", [])
        if isinstance(p, dict) and "text" in p
    ]).strip()

    has_image = False
    for content in body.get("contents", []):
        for part in content.get("parts", []):
            if "text" in part:
                t_val = part["text"]
                prompt_parts.append(t_val)
                yt_urls = extract_youtube_urls(t_val)
                has_file_data_yt = any(
                    isinstance(p, dict) and "file_data" in p and any(u in str(p.get("file_data", {}).get("file_uri", "")) for u in yt_urls)
                    for c in body.get("contents", [])
                    for p in c.get("parts", [])
                )
                if yt_urls and not has_file_data_yt:
                    for y_url in yt_urls:
                        yt_prompt, yt_file = await asyncio.to_thread(
                            process_youtube_request, y_url, user_context or t_val, MEDIA_CACHE_DIR
                        )
                        if yt_file:
                            ephemeral_files.append(yt_file)
                        prompt_parts.append(yt_prompt)
            elif "inline_data" in part or "inlineData" in part:
                idat = part.get("inline_data") or part.get("inlineData", {})
                mime = idat.get("mime_type") or idat.get("mimeType", "image/jpeg")
                b64_str = idat.get("data", "")
                saved_path = _save_base64_media(b64_str, mime) if b64_str else None
                if saved_path:
                    ephemeral_files.append(saved_path)
                    if "image" in mime.lower():
                        has_image = True
                        prompt_parts.append(
                            f"صورة مرفقة محفوظة في المسار التالي على الجهاز:\n{saved_path}\n"
                            "اقرأ هذه الصورة المرفقة في المسار المحدد (باستخدام أداة read_file) وحللها بدقة واستخرج كل النصوص المكتوبة فيها، وقدّم الوصف المطلوب بالصيغة المحددة."
                        )
                    elif "pdf" in mime.lower():
                        prompt_parts.append(
                            f"مستند PDF محلي محفوظ في المسار:\n{saved_path}\n"
                            "اقرأ هذا المستند بدقة باستخدام أداة read_file واستخرج النصوص أو أجب عن السؤال المطلوب."
                        )
                    elif "audio" in mime.lower():
                        prompt_parts.append(
                            f"ملف صوتي محلي محفوظ في المسار:\n{saved_path}\n"
                            "استمع لهذا الملف الصوتي باستخدام أداة read_file وقم بتفريغه وتحويله إلى نص بدقة."
                        )
                    else:
                        prompt_parts.append(
                            f"ملف وسائط محفوظ في المسار:\n{saved_path}\nنوع الملف: {mime}\n"
                            "افحص هذا الملف باستخدام أداة read_file ونفّذ المطلوب بدقة."
                        )
                else:
                    prompt_parts.append(f"[مرفق وسائط: {mime}]")
            elif "file_data" in part or "fileData" in part:
                fdat = part.get("file_data") or part.get("fileData", {})
                f_uri = str(fdat.get("file_uri") or fdat.get("fileUri", "")).strip()
                f_mime = str(fdat.get("mime_type") or fdat.get("mimeType", "")).strip().lower()

                if "youtube.com" in f_uri or "youtu.be" in f_uri:
                    yt_prompt, yt_file = await asyncio.to_thread(
                        process_youtube_request, f_uri, user_context or "فحص وتحليل هذا الفيديو بدقة والإجابة عن طلب المستخدم", MEDIA_CACHE_DIR
                    )
                    if yt_file:
                        ephemeral_files.append(yt_file)
                    prompt_parts.append(yt_prompt)
                elif f_uri.startswith("http://") or f_uri.startswith("https://"):
                    prompt_parts.append(
                        f"رابط ويب مرفق للتحليل:\n{f_uri}\nنوع المحتوى: {f_mime}\n"
                        "المطلوب: قم بزيارة وقراءة محتوى الرابط والإجابة عن طلب المستخدم بدقة."
                    )
                elif f_uri.startswith("/") or f_uri.startswith("file://"):
                    clean_path = f_uri.replace("file://", "")
                    if "pdf" in f_mime or clean_path.lower().endswith(".pdf"):
                        prompt_parts.append(
                            f"مستند PDF محلي موجود في المسار:\n{clean_path}\n"
                            "المطلوب: اقرأ هذا المستند بدقة باستخدام أداة read_file واستخرج النصوص أو أجب عن السؤال المطلوب بناءً على التعليمات."
                        )
                    elif "video" in f_mime or any(clean_path.lower().endswith(ext) for ext in [".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp"]):
                        prompt_parts.append(
                            f"ملف فيديو محلي موجود في المسار:\n{clean_path}\nنوع الملف: {f_mime}\n"
                            "المطلوب: تفقد هذا الملف واقرأ معلوماته ومحتواه وأجب عن طلب المستخدم بدقة."
                        )
                    elif "audio" in f_mime or any(clean_path.lower().endswith(ext) for ext in [".mp3", ".wav", ".m4a", ".aac", ".ogg"]):
                        prompt_parts.append(
                            f"ملف صوتي محلي موجود في المسار:\n{clean_path}\nنوع الملف: {f_mime}\n"
                            "المطلوب: اقرأ هذا الملف الصوتي باستخدام أداة read_file وقم بتفريغه وتحويله إلى نص بدقة عالية."
                        )
                    else:
                        prompt_parts.append(
                            f"ملف محلي مرفق في المسار:\n{clean_path}\nنوع الملف: {f_mime}\n"
                            "المطلوب: افحص هذا الملف باستخدام أداة read_file ونفّذ المطلوب بدقة."
                        )
                else:
                    prompt_parts.append(f"[ملف وسائط مرفق: {f_uri} - نوع: {f_mime}]")

    full_prompt = "\n\n".join(prompt_parts).strip()
    if has_image and ("Description:" in full_prompt or "Format:" in full_prompt or "صف" in full_prompt or "Describe" in full_prompt):
        full_prompt += (
            "\n\nملاحظة هامة جداً: بعد قراءة الصورة عبر read_file، التزم حرفياً بالصيغة التالية في إجابتك لتتمكن الشاشة من قراءتها وعرضها:\n"
            "Description: <اكتب هنا وصف الصورة الكامل بالتفصيل بالعربية>\n"
            "Text: <اكتب هنا كل النصوص المستخرجة بالحرف، أو لا يوجد نص>"
        )

    if not full_prompt:
        full_prompt = "Hello"

    logger.info("gemini-compat: model=%s (resolved=%s) prompt_chars=%d", model_id, resolved_model, len(full_prompt))

    try:
        try:
            res = await _execute_run(full_prompt, resolved_model, None, False, agy_effort)
            answer = res.response.answer
        except Exception as e2:
            logger.error("_execute_run failed: %s", e2)
            err_str = str(e2)
            if any(q in err_str.lower() for q in ["quota", "resource_exhausted", "429"]):
                alt_model = "Gemini 3.8 Flash (Low)" if "medium" in str(resolved_model).lower() else "Claude Sonnet 4.6 (Thinking)"
                logger.info("Auto-fallback on quota exhaustion: switching to %s", alt_model)
                try:
                    res = await _execute_run(full_prompt, alt_model, None, False, None)
                    answer = res.response.answer
                except Exception as e3:
                    logger.error("Auto-fallback to %s also failed: %s", alt_model, e3)
                    answer = "عذراً يا ريس، حدث خطأ مؤقت في الاتصال. يرجى إعادة المحاولة."
            else:
                answer = "عذراً يا ريس، حدث خطأ مؤقت في الاتصال. يرجى إعادة المحاولة."

        return JSONResponse(
            content={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": answer}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 0,
                    "candidatesTokenCount": len(answer),
                    "totalTokenCount": len(answer),
                },
            }
        )
    finally:
        for ef in set(ephemeral_files):
            try:
                ef.unlink(missing_ok=True)
                logger.info("Cleaned up ephemeral media: %s", ef)
            except Exception:
                pass


async def _stream_response(
    prompt: str,
    model: str,
    agy_model: str | None,
    request: Request,
    resume_id: str | None = None,
    keep: bool = False,
    sigs: list[str] | None = None,
    effort: str | None = None,
    cleanup_files: list[Path] | None = None,
) -> AsyncIterator[bytes]:
    started = time.time()
    cid_str = completion_id()
    created_ts = int(started)

    # Real-time token streaming via persistent stream-json worker
    can_use_worker = settings.fast_session and (not resume_id or (worker.is_alive() and worker.cid == resume_id))
    if can_use_worker:
        try:
            yield sse_event(make_role_chunk(cid_str, created_ts, model))
            worker_cid = None
            final_answer = ""
            final_reasoning = ""
            usage = {}

            async for chunk in worker.run_turn_stream(prompt, model=agy_model or "gemini-3.8-flash-low", effort=effort):
                if await request.is_disconnected():
                    logger.warning("client disconnected during real-time stream after %.1fs", time.time() - started)
                    return
                c_type = chunk.get("type")
                if c_type == "delta":
                    d_text = chunk.get("delta", "")
                    if d_text:
                        yield sse_event(make_delta_chunk(cid_str, created_ts, model, d_text))
                elif c_type == "done":
                    worker_cid = chunk.get("cid")
                    final_answer = chunk.get("response", "")
                    final_reasoning = chunk.get("reasoning", "")
                    usage = chunk.get("usage", {})

            if worker_cid and sigs:
                _record_session(worker_cid, sigs)

            yield sse_event(make_finish_chunk(cid_str, created_ts, model, finish_reason="stop"))
            yield sse_event("[DONE]")
            logger.info(
                "ok (real-time stream): model=%s answer_chars=%d cid=%s in %.2fs",
                model, len(final_answer), worker_cid, time.time() - started,
            )
            return
        except Exception as exc:
            logger.warning("Real-time stream failed (%s), falling back to guarded run: %s", type(exc).__name__, exc)

    task = asyncio.create_task(_execute_run(prompt, agy_model, resume_id, keep, effort))
    try:
        while not task.done():
            if await request.is_disconnected():
                logger.warning(
                    "client disconnected after %.1fs; agy run continues in background "
                    "until it finishes (cannot be interrupted mid-print)",
                    time.time() - started,
                )
                task.cancel()
                return
            yield sse_event(make_heartbeat(model))
            await asyncio.sleep(1)

        result = await task
        if sigs:
            _record_session(Path(result.db_path).stem, sigs)
        fr = "length" if result.response.truncated else "stop"
        logger.info(
            "ok (stream): model=%s answer_chars=%d reasoning_chars=%d truncated=%s db=%s resume=%s %.1fs",
            model, len(result.response.answer), len(result.response.reasoning),
            result.response.truncated, result.db_path.name, bool(resume_id), time.time() - started,
        )
        async for item in stream_chunks(result.response.answer, result.response.reasoning, model, finish_reason=fr):
            yield item
    except Exception as exc:
        _forget_session(resume_id)
        logger.error(
            "FAILED (stream): model=%s prompt_chars=%d after %.1fs -> %s",
            model, len(prompt), time.time() - started, exc, exc_info=True,
        )
        yield sse_event({"error": {"message": str(exc), "type": "agy_error"}})
        yield sse_event("[DONE]")
    finally:
        if cleanup_files:
            for f in set(cleanup_files):
                try:
                    f.unlink(missing_ok=True)
                    logger.info("Cleaned up ephemeral file %s", f)
                except Exception:
                    pass


def format_messages(messages: list[Any], ephemeral_files: list[Path] | None = None) -> str:
    lines: list[str] = []
    for message in messages:
        role = getattr(message, "role", "user")
        content = _content_to_text(getattr(message, "content", ""), ephemeral_files=ephemeral_files)
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines).strip()


def _content_to_text(
    content: Any,
    ephemeral_files: list[Path] | None = None,
    save_media: bool = True,
) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t == "text":
                    pieces.append(str(item.get("text", "")))
                elif "text" in item:
                    pieces.append(str(item["text"]))
                elif t == "image_url":
                    img_info = item.get("image_url", {})
                    url = img_info.get("url", "") if isinstance(img_info, dict) else str(img_info)
                    if url.startswith("data:"):
                        if save_media:
                            saved_path = _save_base64_media(url)
                            if saved_path:
                                if ephemeral_files is not None:
                                    ephemeral_files.append(saved_path)
                                pieces.append(
                                    f"صورة مرفقة محفوظة في المسار التالي على الجهاز:\n{saved_path}\n"
                                    "اقرأ هذه الصورة المرفقة في المسار المحدد (باستخدام أداة read_file) وحللها بدقة واستخرج كل النصوص المكتوبة فيها."
                                )
                            else:
                                pieces.append("[مرفق صورة base64]")
                        else:
                            pieces.append("[مرفق صورة base64]")
                    elif url.startswith("http://") or url.startswith("https://"):
                        pieces.append(f"صورة عبر الرابط: {url}\nيرجى فحصها وتحليلها.")
                    elif url.startswith("/") or url.startswith("file://"):
                        clean_path = url.replace("file://", "")
                        pieces.append(f"صورة في المسار التالي على الجهاز:\n{clean_path}\nاقرأ هذه الصورة باستخدام read_file وحللها بدقة.")
            else:
                pieces.append(str(item))
        return "\n".join(piece for piece in pieces if piece)
    return str(content)


def build_completion_response(
    answer: str,
    reasoning: str,
    model: str,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> dict:
    created = int(time.time())
    message: dict[str, Any] = {"role": "assistant", "content": answer}
    if settings.expose_reasoning and reasoning:
        message["reasoning_content"] = reasoning

    prompt_tokens = usage.get("input_tokens", 0) if usage else 0
    completion_tokens = usage.get("output_tokens", len(answer)) if usage else len(answer)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens) if usage else len(answer)

    return {
        "id": f"chatcmpl-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "agy_error"}},
    )


def _enforce_bind_safety() -> None:
    if is_loopback_host(settings.host) or settings.allow_remote:
        return
    import sys

    sys.stderr.write(
        f"\nRefusing to bind non-loopback host {settings.host!r}.\n"
        "Exposing this endpoint shares your personal Google quota with anyone\n"
        "who can reach it. For personal/local use keep HOST=127.0.0.1.\n"
        "If you truly intend remote access, set AGY2API_ALLOW_REMOTE=1 and a\n"
        "strong AGY2API_KEY, and accept that callers run prompts under your\n"
        "Google account.\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    import uvicorn

    _enforce_bind_safety()
    uvicorn.run("server:app", host=settings.host, port=settings.port, reload=False)
