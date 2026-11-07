"""Background registration jobs for the panel.

Registration takes tens of seconds per account (inbox, sign-up, verification
mail, key minting), far too long to hold an HTTP request open.  Each panel run
becomes a subprocess of the existing CLI, so the panel adds orchestration
without duplicating the pipeline.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_LINES = 400


@dataclass
class Job:
    id: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lines: list[str] = field(default_factory=list)
    created: int = 0
    failed: int = 0

    def log(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            del self.lines[: len(self.lines) - MAX_LINES]

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lines": self.lines[-200:],
            "created": self.created,
            "failed": self.failed,
        }


class JobManager:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        return [job.public() for job in sorted(self.jobs.values(), key=lambda j: j.started_at, reverse=True)]

    def start(self, args: list[str]) -> Job:
        job = Job(id=secrets.token_hex(6))
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job, args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, args: list[str]) -> None:
        command = [sys.executable, "-m", "th2api.registration.cli", *args]
        job.log(f"$ {' '.join(command[1:])}")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.home),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env(),
            )
        except OSError as exc:
            job.status = "failed"
            job.finished_at = time.time()
            job.log(f"could not start registration: {exc}")
            return

        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            if line.startswith("  api key:"):
                job.created += 1
                # Never keep the plaintext key in a log line a browser can read
                # back; the record already holds it.
                line = "  api key: (saved to var/accounts.jsonl)"
            if "registration failed" in line:
                job.failed += 1
            job.log(line)

        code = await process.wait()
        job.status = "completed" if code == 0 else "failed"
        job.finished_at = time.time()
        job.log(f"exit code {code}")

    def _env(self) -> dict[str, str]:
        import os

        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env