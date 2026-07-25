import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class ApplyPipelineJob:
    project_id: int
    mr_iid: int
    sha: str
    pr_url: str
    data: dict
    log_context: dict
    commands_conf: str

    @property
    def key(self) -> tuple[int, int]:
        return self.project_id, self.mr_iid


class ApplyPipelineCoordinator:
    def __init__(self, claim_ttl_seconds: int = 600,
                 clock: Callable[[], float] = time.monotonic):
        self._claim_ttl_seconds = claim_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._claims: dict[tuple[int, str], float] = {}
        self._running: dict[tuple[int, int], ApplyPipelineJob] = {}
        self._pending: dict[tuple[int, int], ApplyPipelineJob] = {}

    def _expire_claims(self, now: float) -> None:
        expired = [
            claim_key for claim_key, claimed_at in self._claims.items()
            if now - claimed_at > self._claim_ttl_seconds
        ]
        for claim_key in expired:
            del self._claims[claim_key]

    def enqueue(self, job: ApplyPipelineJob) -> str:
        with self._lock:
            now = self._clock()
            self._expire_claims(now)
            claim_key = (job.project_id, job.sha)
            if claim_key in self._claims:
                return "duplicate"
            self._claims[claim_key] = now
            if job.key not in self._running:
                self._running[job.key] = job
                return "start"
            self._pending[job.key] = job
            return "queued"

    def complete(self, job: ApplyPipelineJob) -> Optional[ApplyPipelineJob]:
        with self._lock:
            if self._running.get(job.key) != job:
                return None
            next_job = self._pending.pop(job.key, None)
            if next_job is None:
                self._running.pop(job.key, None)
                return None
            self._running[job.key] = next_job
            return next_job
