"""Single-Worker Isolated Session Manager for agy2api.

Maintains a SINGLE interactive `agy` CLI session running in `isolated_workspace`
and `isolated_home` using the official Google Antigravity binary and `token_884.json`.

Features:
- Fast sub-2.5s response times via persistent stream-json.
- Zero dual-process swapping: only 1 process in RAM.
- Clean 30-minute auto-recycling (TTL = 1800s): starts a fresh, empty session
  every 30 minutes to strictly prevent context accumulation and slowdowns.
- Smart idle sleep: shuts down after 10 minutes of complete inactivity to save battery.
- Automatic conversation cleanup on recycle: wipes temporary SQLite and brain files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Tuple

from agy_runner import AgyRunResult
from config import settings
from conversation_reader import AgResponse

logger = logging.getLogger("agy2api")
REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR = REPO_ROOT / "isolated_workspace"
ISOLATED_HOME = REPO_ROOT / "isolated_home"


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
        if (time.time() - self.created_at) >= self.session_ttl:
            logger.info("IsolatedWorker: 30-minute TTL reached (age=%.1fs >= %.1fs)", time.time() - self.created_at, self.session_ttl)
            return True
        if self.turn_count >= self.max_turns:
            logger.info("IsolatedWorker: max turns reached (%d >= %d)", self.turn_count, self.max_turns)
            return True
        if model != self.current_model or effort != self.current_effort:
            logger.info("IsolatedWorker: model or effort changed (%s/%s -> %s/%s)", self.current_model, self.current_effort, model, effort)
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
                old_proc.terminate()
                await asyncio.wait_for(old_proc.wait(), timeout=2.0)
            except Exception:
                try:
                    old_proc.kill()
                    await asyncio.wait_for(old_proc.wait(), timeout=1.0)
                except Exception:
                    pass

        if cleanup and old_cid:
            await self._cleanup_cid(old_cid)

    async def get_or_spawn(self, model: str, effort: str | None) -> asyncio.subprocess.Process:
        if self.should_recycle(model, effort):
            await self.stop(cleanup=True)

        if self.is_alive():
            return self.proc

        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        ISOLATED_HOME.mkdir(parents=True, exist_ok=True)

        cmd = [
            settings.agy_path,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
        ]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])

        env = settings.agy_env()
        t0 = time.time()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(WORKSPACE_DIR),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
        self.current_model = model
        self.current_effort = effort

        logger.info(
            "IsolatedWorker: spawned fresh session (PID=%s, CID=%s, model=%s, effort=%s in %.2fs)",
            self.proc.pid, self.cid, model, effort, time.time() - t0,
        )
        return self.proc

    async def run_turn(
        self,
        prompt: str,
        model: str = "gemini-3.8-flash-low",
        effort: str | None = None,
        timeout: float = 60.0,
    ) -> Tuple[str, str]:
        async with self.lock:
            proc = await self.get_or_spawn(model, effort)
            t0 = time.time()
            msg = json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"
            proc.stdin.write(msg.encode("utf-8"))
            await proc.stdin.drain()

            accumulated_response = ""
            accumulated_reasoning = ""

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
                if event == "result":
                    res = data.get("result", {})
                    if res.get("status") == "SUCCESS":
                        accumulated_response = res.get("response", "")
                    else:
                        err_msg = res.get("error", "unknown error")
                        await self.stop(cleanup=True)
                        raise RuntimeError(f"agy turn error: {err_msg}")
                    break

            dur = time.time() - t0
            self.turn_count += 1
            self.last_used_at = time.time()

            logger.info(
                "IsolatedWorker: turn %d completed in %.2fs (CID: %s, chars: %d)",
                self.turn_count, dur, self.cid, len(accumulated_response),
            )
            return accumulated_response, accumulated_reasoning

    async def run_turn_result(
        self,
        prompt: str,
        model: str = "gemini-3.8-flash-low",
        effort: str | None = None,
        timeout: float = 60.0,
    ) -> AgyRunResult:
        answer, reasoning = await self.run_turn(prompt, model=model, effort=effort, timeout=timeout)
        db_file = (ISOLATED_HOME / ".gemini" / "antigravity-cli" / "conversations" / f"{self.cid}.db") if self.cid else Path("session.db")
        return AgyRunResult(
            db_path=db_file,
            response=AgResponse(answer=answer, reasoning=reasoning, truncated=False, db_path=str(db_file)),
        )

    async def _sweeper_loop(self):
        while True:
            try:
                await asyncio.sleep(15.0)
                async with self.lock:
                    if self.is_alive():
                        now = time.time()
                        if now - self.last_used_at >= self.idle_timeout:
                            logger.info("IsolatedWorker: idle timeout reached (%.0fs), sleeping to save RAM", now - self.last_used_at)
                            await self.stop(cleanup=True)
                        elif now - self.created_at >= self.session_ttl:
                            logger.info("IsolatedWorker: 30-min TTL reached (%.0fs), recycling session", now - self.created_at)
                            await self.stop(cleanup=True)
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
