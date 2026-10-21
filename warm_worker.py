"""Warm Agy Worker Daemon for agy2api.

Keeps a single official `agy` CLI process alive in memory using the built-in
`--input-format stream-json --output-format stream-json` mode.

Eliminates the 20-second Node.js / glibc / MCP startup overhead while executing
100% through the official Google Antigravity binary (`agy`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger("agy2api")

ISOLATED_HOME = Path.home() / "agy2api" / "isolated_home"
EMPTY_WS = Path.home() / "agy2api" / "empty_ws"


class WarmAgyWorker:
    """Manages a long-running, warm agy CLI process in stream-json mode."""

    def __init__(self, model: str = "gemini-3.8-flash-low"):
        self.model = model
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.conversation_id: str | None = None
        self._starting = False

    def _setup_sandbox(self) -> dict[str, str]:
        ISOLATED_HOME.mkdir(parents=True, exist_ok=True)
        EMPTY_WS.mkdir(parents=True, exist_ok=True)

        target_dir = ISOLATED_HOME / ".gemini" / "antigravity-cli"
        target_dir.mkdir(parents=True, exist_ok=True)

        token_src = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token_dst = target_dir / "antigravity-oauth-token"
        if token_src.exists() and (not token_dst.exists() or token_src.stat().st_mtime > token_dst.stat().st_mtime):
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

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if self._starting:
            return
        self._starting = True

        env = self._setup_sandbox()
        cmd = [
            "agy",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--model", self.model,
            "--add-dir", str(EMPTY_WS),
        ]

        logger.info("WarmAgyWorker: Starting official agy binary in stream-json mode...")
        t0 = time.time()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(EMPTY_WS),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait for init event from agy
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=30.0)
                if not line:
                    break
                try:
                    data = json.loads(line.decode("utf-8", errors="replace"))
                    if data.get("event") == "init":
                        self.conversation_id = data.get("conversation_id")
                        logger.info(
                            "WarmAgyWorker: Initialized in %.2fs (CID: %s)",
                            time.time() - t0,
                            self.conversation_id,
                        )
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.error("WarmAgyWorker start failed: %s", e)
            await self.stop()
        finally:
            self._starting = False

    async def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            self.conversation_id = None

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def run_turn(self, prompt: str, model: str | None = None) -> str:
        async with self.lock:
            if model and model != self.model:
                logger.info("WarmAgyWorker: switching model from %s to %s", self.model, model)
                await self.stop()
                self.model = model
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

            logger.info("WarmAgyWorker: Turn completed in %.2fs", time.time() - t0)
            return accumulated_text.strip()


warm_worker = WarmAgyWorker()
