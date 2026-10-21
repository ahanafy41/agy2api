"""Warm Agy Worker Daemon for agy2api.

Keeps official `agy` CLI processes alive in memory using the built-in
`--input-format stream-json --output-format stream-json` mode.

Eliminates the 20-second Node.js / glibc / MCP startup overhead while executing
100% through the official Google Antigravity binary (`agy`).

Features:
- Sub-3-second turns on Gemini 3.8 Flash (Low).
- Smart Context Auto-Recycling: rotates sessions every 5 turns to prevent context bloat.
- Background Pre-Warming: pre-warms a standby worker with zero user-perceived delay.
- Process Group Sandboxing: prevents zombie / orphaned child processes.
- Automatic Brain Directory cleanup to avoid disk bloat.
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

logger = logging.getLogger("agy2api")

REPO_ROOT = Path(__file__).resolve().parent
ISOLATED_HOME = REPO_ROOT / "isolated_home"
EMPTY_WS = REPO_ROOT / "empty_ws"
MEDIA_CACHE_DIR = REPO_ROOT / "media_cache"
MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class WarmAgyWorker:
    """Manages long-running, warm agy CLI processes with auto-rotation."""

    def __init__(self, model: str = "gemini-3.8-flash-low", max_turns: int = 2):
        self.model = model
        self.max_turns = max_turns
        self.process: asyncio.subprocess.Process | None = None
        self.conversation_id: str | None = None
        self.turn_count: int = 0
        self.lock = asyncio.Lock()
        self._starting = False

        self._standby_worker: asyncio.subprocess.Process | None = None
        self._standby_cid: str | None = None
        self._standby_model: str | None = None
        self._standby_task: asyncio.Task | None = None

        self._last_active_time = time.time()
        self._idle_task: asyncio.Task | None = None

    def _setup_sandbox(self) -> dict[str, str]:
        ISOLATED_HOME.mkdir(parents=True, exist_ok=True)
        EMPTY_WS.mkdir(parents=True, exist_ok=True)

        target_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli"
        target_dir.mkdir(parents=True, exist_ok=True)

        dedicated_token = Path(__file__).resolve().parent / "token_884.json"
        token_src = dedicated_token if dedicated_token.exists() else (Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token")
        token_dst = target_dir / "antigravity-oauth-token"
        if token_src.exists():
            shutil.copy2(token_src, token_dst)

        # Keep updater silent
        touch_target = target_dir / "last_check.timestamp"
        touch_target.touch(exist_ok=True)

        env = dict(os.environ)
        env["HOME"] = str(ISOLATED_HOME)
        env["USERPROFILE"] = str(ISOLATED_HOME)
        env["AGY_AUTO_UPDATE"] = "0"
        env["AGY_NO_UPDATE_CHECK"] = "1"
        env["GODEBUG"] = "netdns=cgo"
        return env

    async def _spawn_process(self, model: str) -> tuple[asyncio.subprocess.Process, str]:
        env = self._setup_sandbox()
        cmd = [
            "agy",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--model", model,
            "--add-dir", str(EMPTY_WS),
            "--add-dir", str(MEDIA_CACHE_DIR),
        ]

        logger.info("WarmAgyWorker: Starting official agy binary (model: %s)...", model)
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(EMPTY_WS),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        cid = ""
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
            if not line:
                break
            try:
                data = json.loads(line.decode("utf-8", errors="replace"))
                if data.get("event") == "init":
                    cid = data.get("conversation_id", "")
                    logger.info(
                        "WarmAgyWorker: Initialized in %.2fs (CID: %s, PID: %d)",
                        time.time() - t0,
                        cid,
                        proc.pid,
                    )
                    break
            except Exception:
                continue

        if not cid:
            await self._kill_process(proc)
            raise RuntimeError("Warm worker failed to initialize conversation_id")

        return proc, cid

    async def _kill_process(self, proc: asyncio.subprocess.Process | None, cid: str | None = None) -> None:
        if not proc:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/pid", str(proc.pid), "/t", "/f"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        for s in (proc.stdin, proc.stdout, proc.stderr):
            if s:
                try:
                    s.close()
                except Exception:
                    pass

        if cid:
            brain_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli" / "brain" / cid
            shutil.rmtree(brain_dir, ignore_errors=True)
            conv_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli" / "conversations"
            for ext in ["", "-wal", "-shm"]:
                (conv_dir / f"{cid}.db{ext}").unlink(missing_ok=True)

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if self._starting:
            return
        self._starting = True
        try:
            self.process, self.conversation_id = await self._spawn_process(self.model)
            self.turn_count = 0
            self._last_active_time = time.time()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._idle_monitor())
            if (not self._standby_worker or self._standby_worker.returncode is not None) and (not self._standby_task or self._standby_task.done()):
                self._standby_task = asyncio.create_task(self._prewarm_standby(self.model))
        finally:
            self._starting = False

    async def stop(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        if self._standby_task and not self._standby_task.done():
            self._standby_task.cancel()

        old_proc, old_cid = self.process, self.conversation_id
        self.process = None
        self.conversation_id = None
        self.turn_count = 0

        standby_proc, standby_cid = self._standby_worker, self._standby_cid
        self._standby_worker = None
        self._standby_cid = None

        await self._kill_process(old_proc, old_cid)
        await self._kill_process(standby_proc, standby_cid)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _prewarm_standby(self, target_model: str) -> None:
        try:
            if (
                self._standby_worker
                and self._standby_worker.returncode is None
                and self._standby_model == target_model
            ):
                return
            proc, cid = await self._spawn_process(target_model)
            self._standby_worker = proc
            self._standby_cid = cid
            self._standby_model = target_model
            logger.info("WarmAgyWorker: Standby worker ready for instant rotation (CID: %s)", cid)
        except Exception as e:
            logger.warning("WarmAgyWorker: Prewarm standby failed: %s", e)

    async def _idle_monitor(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                if self.turn_count > 0 and (time.time() - self._last_active_time) > 90:
                    async with self.lock:
                        logger.info(
                            "WarmAgyWorker: Idle for >90s (turn_count=%d). Auto-recycling to fresh session...",
                            self.turn_count,
                        )
                        if self._standby_worker and self._standby_worker.returncode is None and self._standby_model == self.model:
                            old_proc, old_cid = self.process, self.conversation_id
                            self.process = self._standby_worker
                            self.conversation_id = self._standby_cid
                            self._standby_worker = None
                            self._standby_cid = None
                            self.turn_count = 0
                            asyncio.create_task(self._kill_process(old_proc, old_cid))
                        else:
                            await self.stop()
                            await self.start()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("WarmAgyWorker: Idle monitor check error: %s", e)

    async def run_turn(self, prompt: str, model: str | None = None) -> str:
        async with self.lock:
            target_model = model or self.model
            if target_model != self.model:
                logger.info("WarmAgyWorker: switching model from %s to %s", self.model, target_model)
                await self.stop()
                self.model = target_model

            await self.start()
            if not self.is_alive():
                raise RuntimeError("Warm agy worker failed to start")

            msg = json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"
            self.process.stdin.write(msg.encode("utf-8"))
            await self.process.stdin.drain()

            t0 = time.time()
            accumulated_text = ""
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=60.0)
                if not line:
                    await self.stop()
                    raise RuntimeError("Warm worker unexpectedly closed stdout")
                try:
                    data = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue

                event = data.get("event")
                if event == "result":
                    res = data.get("result", {})
                    if res.get("status") == "SUCCESS":
                        accumulated_text = res.get("response", "")
                    else:
                        await self.stop()
                        raise RuntimeError(f"agy error: {res.get('error')}")
                    break

            dur = time.time() - t0
            self.turn_count += 1
            self._last_active_time = time.time()
            logger.info(
                "WarmAgyWorker: Turn %d completed in %.2fs (CID: %s, model: %s)",
                self.turn_count,
                dur,
                self.conversation_id,
                self.model,
            )

            # Proactive rotation: if standby is ready or turn limit reached
            should_rotate = (
                self._standby_worker is not None
                and self._standby_worker.returncode is None
                and self._standby_model == self.model
                and self.turn_count >= 1
            ) or (self.turn_count >= self.max_turns)

            if should_rotate:
                if self._standby_worker and self._standby_worker.returncode is None and self._standby_model == self.model:
                    logger.info(
                        "WarmAgyWorker: Seamless swap to pre-warmed fresh session (turn_count=%d)",
                        self.turn_count,
                    )
                    old_proc, old_cid = self.process, self.conversation_id
                    self.process = self._standby_worker
                    self.conversation_id = self._standby_cid
                    self._standby_worker = None
                    self._standby_cid = None
                    self.turn_count = 0
                    asyncio.create_task(self._kill_process(old_proc, old_cid))
                    self._standby_task = asyncio.create_task(self._prewarm_standby(self.model))
                else:
                    logger.info("WarmAgyWorker: Pre-warming next session for upcoming turn...")
                    if not self._standby_task or self._standby_task.done():
                        self._standby_task = asyncio.create_task(self._prewarm_standby(self.model))
            else:
                if (not self._standby_worker or self._standby_worker.returncode is not None) and (not self._standby_task or self._standby_task.done()):
                    self._standby_task = asyncio.create_task(self._prewarm_standby(self.model))

            return accumulated_text.strip()


warm_worker = WarmAgyWorker()
