"""Single-Worker Isolated Session Manager for agy2api.

Maintains a SINGLE interactive `agy` CLI session running in `isolated_workspace`
and `isolated_home` using the official Google Antigravity binary and synchronized tokens.

Features:
- Fast sub-2.5s response times via persistent stream-json.
- Real-time native token streaming (SSE) via `run_turn_stream`.
- Real token usage extraction (`input_tokens`, `output_tokens`).
- Zero dual-process swapping: only 1 process in RAM.
- Clean auto-recycling (TTL = 1800s, max_turns protection): strictly prevents context accumulation and slowdowns.
- Automatic OAuth token synchronization from dedicated or system location.
- Robust process-group sandboxing (`start_new_session=True` & `os.killpg`) to eliminate zombie processes.
- Smart idle sleep: shuts down after 10 minutes of complete inactivity to save battery.
- Automatic conversation cleanup on recycle: wipes temporary SQLite and brain files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Tuple

from agy_runner import AgyRunResult
from config import settings
from conversation_reader import AgResponse

logger = logging.getLogger("agy2api")
REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR = REPO_ROOT / "isolated_workspace"
ISOLATED_HOME = REPO_ROOT / "isolated_home"


def _ensure_token_synced() -> None:
    """Ensure isolated_home has the dedicated or active OAuth token and disabled MCP config."""
    target_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli"
    target_dir.mkdir(parents=True, exist_ok=True)
    mcp_file = target_dir / "mcp_config.json"
    if not mcp_file.exists():
        try:
            mcp_file.write_text('{"mcpServers": {}}', encoding="utf-8")
        except Exception:
            pass
    dedicated_token = REPO_ROOT / "token_884.json"
    system_token = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_src = dedicated_token if dedicated_token.exists() else (system_token if system_token.exists() else None)
    token_dst = target_dir / "antigravity-oauth-token"
    if token_src and token_src.exists():
        if not token_dst.exists() or token_src.stat().st_mtime > token_dst.stat().st_mtime:
            try:
                shutil.copy2(token_src, token_dst)
                logger.debug("Synced oauth token from %s to %s", token_src, token_dst)
            except Exception as exc:
                logger.warning("Failed syncing oauth token: %s", exc)


class IsolatedWorker:
    def __init__(
        self,
        session_ttl: float | None = None,
        max_turns: int | None = None,
        idle_timeout: float | None = None,
    ):
        self.session_ttl = session_ttl if session_ttl is not None else settings.session_ttl
        self.max_turns = max_turns if max_turns is not None else settings.max_turns
        self.idle_timeout = idle_timeout if idle_timeout is not None else settings.idle_timeout

        self.proc: asyncio.subprocess.Process | None = None
        self.cid: str | None = None
        self.created_at: float = 0.0
        self.last_used_at: float = 0.0
        self.turn_count: int = 0
        self.current_model: str = ""
        self.current_effort: str | None = None

        self.lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task | None = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def should_recycle(self, model: str, effort: str | None) -> bool:
        if not self.is_alive():
            return True
        if self.session_ttl > 0 and (time.time() - self.created_at) >= self.session_ttl:
            logger.info("IsolatedWorker: 30-minute TTL reached (age=%.1fs >= %.1fs)", time.time() - self.created_at, self.session_ttl)
            return True

        if self.turn_count >= self.max_turns:
            logger.info("IsolatedWorker: max turns reached (%d >= %d)", self.turn_count, self.max_turns)
            return True

        from config import resolve_model
        req_m, req_e = resolve_model(model, effort)
        cur_m, cur_e = resolve_model(self.current_model, self.current_effort) if self.current_model else (None, None)
        if req_m != cur_m or req_e != cur_e:
            logger.info("IsolatedWorker: model or effort changed (%s/%s -> %s/%s)", cur_m, cur_e, req_m, req_e)
            return True
        return False

    async def _cleanup_cid(self, cid: str | None):
        if not cid:
            return
        conv_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli" / "conversations"
        db_file = conv_dir / f"{cid}.db"
        for suffix in ("", "-wal", "-shm"):
            f = conv_dir / f"{cid}.db{suffix}"
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass
        brain_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli" / "brain" / cid
        if brain_dir.exists():
            try:
                shutil.rmtree(brain_dir, ignore_errors=True)
            except Exception:
                pass

    async def stop(self, cleanup: bool = True):
        old_proc = self.proc
        old_cid = self.cid
        self.proc = None
        self.cid = None
        self.created_at = 0.0
        self.turn_count = 0

        if old_proc and old_proc.returncode is None:
            try:
                if os.name != "nt" and old_proc.pid:
                    try:
                        os.killpg(os.getpgid(old_proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        old_proc.terminate()
                else:
                    old_proc.terminate()
                await asyncio.wait_for(old_proc.wait(), timeout=2.0)
            except Exception:
                try:
                    if os.name != "nt" and old_proc.pid:
                        try:
                            os.killpg(os.getpgid(old_proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, OSError):
                            old_proc.kill()
                    else:
                        old_proc.kill()
                    await asyncio.wait_for(old_proc.wait(), timeout=1.0)
                except Exception:
                    pass

        if cleanup and old_cid:
            await self._cleanup_cid(old_cid)

    async def get_or_spawn(self, model: str = "gemini-3.8-flash-low", effort: str | None = None) -> asyncio.subprocess.Process:
        from config import resolve_model
        resolved_model, resolved_effort = resolve_model(model, effort)
        if self.should_recycle(resolved_model, resolved_effort):
            await self.stop(cleanup=True)

        if self.is_alive():
            return self.proc

        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        ISOLATED_HOME.mkdir(parents=True, exist_ok=True)
        _ensure_token_synced()

        cmd = [
            settings.agy_path,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
        ]
        if resolved_model:
            cmd.extend(["--model", resolved_model])
        if resolved_effort:
            cmd.extend(["--effort", resolved_effort])

        env = settings.agy_env()
        t0 = time.time()
        extra_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            extra_kwargs["start_new_session"] = True
        else:
            extra_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(WORKSPACE_DIR),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **extra_kwargs,
        )

        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=15.0)
            if not line:
                raise RuntimeError("agy closed stdout unexpectedly on init")
            data = json.loads(line.decode("utf-8", errors="replace"))
            self.cid = data.get("conversation_id")
        except Exception as exc:
            await self.stop(cleanup=True)
            raise RuntimeError(f"Failed to initialize agy worker: {exc}") from exc

        self.created_at = time.time()
        self.last_used_at = time.time()
        self.turn_count = 0
        self.current_model = resolved_model
        self.current_effort = resolved_effort

        logger.info(
            "IsolatedWorker: spawned fresh session (PID=%s, CID=%s, model=%s, effort=%s in %.2fs)",
            self.proc.pid, self.cid, resolved_model, resolved_effort, time.time() - t0,
        )
        return self.proc

    async def _recycle_and_prewarm(self, model: str, effort: str | None) -> None:
        async with self.lock:
            if self.turn_count >= self.max_turns or not self.is_alive():
                logger.info(
                    "IsolatedWorker: reached turn limit (%d >= %d), auto-recycling and pre-warming standby instance",
                    self.turn_count, self.max_turns,
                )
                await self.stop(cleanup=True)
                await self.get_or_spawn(model, effort)

    async def run_turn_stream(
        self,
        prompt: str,
        model: str = "gemini-3.8-flash-low",
        effort: str | None = None,
        timeout: float = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streams real token deltas as emitted by agy in stream-json mode.

        Yields:
            {"type": "delta", "delta": "..."}
            {"type": "done", "response": "...", "reasoning": "...", "usage": {...}, "cid": "...", "duration": float}
        """
        should_prewarm = False
        pw_model = ""
        pw_effort = None
        try:
            async with self.lock:
                proc = await self.get_or_spawn(model, effort)
                t0 = time.time()
                msg = json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"
                proc.stdin.write(msg.encode("utf-8"))
                await proc.stdin.drain()

                accumulated_response = ""
                accumulated_reasoning = ""
                usage: dict[str, int] = {}

                deadline = time.time() + timeout
                while time.time() < deadline:
                    remaining = max(1.0, deadline - time.time())
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                    if not line:
                        await self.stop(cleanup=True)
                        raise RuntimeError("Worker process terminated unexpectedly during turn")
                    try:
                        data = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue

                    event = data.get("event")
                    if event == "step_update":
                        su = data.get("step_update", {})
                        step_type = su.get("step_type")
                        if step_type == "agent_response" and "text_delta" in su:
                            delta = su["text_delta"]
                            accumulated_response += delta
                            yield {"type": "delta", "delta": delta}
                        if "usage" in su and isinstance(su["usage"], dict):
                            usage = su["usage"]

                    elif event == "result":
                        res = data.get("result", {})
                        if res.get("status") == "SUCCESS":
                            final_resp = res.get("response", "")
                            if not accumulated_response and final_resp:
                                accumulated_response = final_resp
                                yield {"type": "delta", "delta": final_resp}
                            if "usage" in res and isinstance(res["usage"], dict):
                                usage = res["usage"]
                        else:
                            err_msg = res.get("error", "unknown error")
                            await self.stop(cleanup=True)
                            raise RuntimeError(f"agy turn error: {err_msg}")
                        break

                dur = time.time() - t0
                self.turn_count += 1
                self.last_used_at = time.time()

                if self.turn_count >= self.max_turns:
                    should_prewarm = True
                    pw_model = self.current_model or model
                    pw_effort = self.current_effort or effort

                logger.info(
                    "IsolatedWorker: turn %d completed in %.2fs (CID: %s, chars: %d)",
                    self.turn_count, dur, self.cid, len(accumulated_response),
                )
                yield {
                    "type": "done",
                    "response": accumulated_response,
                    "reasoning": accumulated_reasoning,
                    "usage": usage,
                    "cid": self.cid,
                    "duration": dur,
                }
        finally:
            if should_prewarm:
                asyncio.create_task(self._recycle_and_prewarm(pw_model, pw_effort))

    async def run_turn(
        self,
        prompt: str,
        model: str = "gemini-3.8-flash-low",
        effort: str | None = None,
        timeout: float = 60.0,
    ) -> Tuple[str, str]:
        res = await self.run_turn_result(prompt, model=model, effort=effort, timeout=timeout)
        return res.response.answer, res.response.reasoning

    async def run_turn_result(
        self,
        prompt: str,
        model: str = "gemini-3.8-flash-low",
        effort: str | None = None,
        timeout: float = 60.0,
    ) -> AgyRunResult:
        answer = ""
        reasoning = ""
        usage: dict[str, int] = {}
        async for chunk in self.run_turn_stream(prompt, model=model, effort=effort, timeout=timeout):
            if chunk["type"] == "done":
                answer = chunk["response"]
                reasoning = chunk["reasoning"]
                usage = chunk.get("usage", {})

        db_file = (ISOLATED_HOME / ".gemini" / "antigravity-cli" / "conversations" / f"{self.cid}.db") if self.cid else Path("session.db")
        return AgyRunResult(
            db_path=db_file,
            response=AgResponse(
                answer=answer,
                reasoning=reasoning,
                truncated=False,
                db_path=str(db_file),
                usage=usage,
            ),
        )

    async def _sweeper_loop(self):
        while True:
            try:
                await asyncio.sleep(15.0)
                re_warm_model = None
                re_warm_effort = None
                async with self.lock:
                    if self.is_alive():
                        now = time.time()
                        if self.idle_timeout > 0 and (now - self.last_used_at >= self.idle_timeout):
                            logger.info("IsolatedWorker: idle timeout reached (%.0fs), sleeping to save RAM", now - self.last_used_at)
                            await self.stop(cleanup=True)
                        elif self.session_ttl > 0 and (now - self.created_at >= self.session_ttl):
                            logger.info("IsolatedWorker: 30-min TTL reached (%.0fs), recycling session and auto-warming in background", now - self.created_at)
                            re_warm_model = self.current_model or "gemini-3.8-flash-low"
                            re_warm_effort = self.current_effort
                            await self.stop(cleanup=True)

                if re_warm_model:
                    asyncio.create_task(self.get_or_spawn(re_warm_model, re_warm_effort))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("IsolatedWorker: sweeper error: %s", e)

    def start_sweeper(self):
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(self._sweeper_loop())

    def stop_sweeper(self):
        if self._sweeper_task and not self._sweeper_task.done():
            self._sweeper_task.cancel()


# Global singleton instance
worker = IsolatedWorker()
