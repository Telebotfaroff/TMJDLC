"""Durable local SQLite queue with GitHub-backed snapshots and upload history."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from tmjdlc.github_store import GitHubDataStore

log = logging.getLogger("tmjdlc.queue")


class QueueNotConfigured(RuntimeError):
    pass


def _sqlite_sql(query: str) -> str:
    """Translate the small PostgreSQL SQL subset used by the queue to SQLite."""
    query = query.replace("now()+(%s*interval '1 second')",
                          "datetime('now', printf('%+d seconds', %s))")
    query = query.replace("UPDATE tmjdlc_bot_jobs j SET", "UPDATE tmjdlc_bot_jobs AS j SET")
    query = query.replace("pg_advisory_xact_lock(734821)", "1")
    query = query.replace("RETURNING j.*", "RETURNING *")
    query = query.replace(" FOR UPDATE SKIP LOCKED", "")
    query = query.replace(" FOR UPDATE", "")
    query = query.replace("::jsonb", "")
    query = query.replace("now()", "CURRENT_TIMESTAMP")
    return query.replace("%s", "?")


class _SQLiteSession:
    """One serialized SQLite transaction, safe for the bot's threaded workers."""

    def __init__(self, path: Path, lock: threading.RLock, owner: "JobQueue"):
        self.path = path
        self.lock = lock
        self.owner = owner
        self.connection: sqlite3.Connection | None = None

    def __enter__(self):
        self.lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(self.path), timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.execute("BEGIN IMMEDIATE")
            return self
        except Exception:
            if self.connection is not None:
                self.connection.close()
            self.lock.release()
            raise

    def execute(self, query: str, params=()):
        if self.connection is None:
            raise RuntimeError("SQLite session is not active.")
        return self.connection.execute(_sqlite_sql(query), params)

    def __exit__(self, exc_type, exc, traceback):
        changed = False
        try:
            if self.connection is not None:
                changed = self.connection.total_changes > 0
                try:
                    if exc_type is None:
                        self.connection.commit()
                    else:
                        self.connection.rollback()
                finally:
                    self.connection.close()
        finally:
            self.connection = None
            self.lock.release()
        if exc_type is None and changed:
            self.owner._maybe_sync_snapshot()
        return False


class JobQueue:
    """SQLite queue with GitHub snapshots; one active Colab runtime is supported."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        store: GitHubDataStore | None = None,
        max_attempts: int = 3,
    ) -> None:
        # dsn remains accepted for a clearer migration error for old notebooks.
        if dsn:
            raise QueueNotConfigured("DATABASE_URL is no longer used; configure GITHUB_TOKEN.")
        self.store = store or GitHubDataStore.from_env()
        self.db_path = Path(os.getenv("TMJDLC_DB_PATH", "/content/tmjdlc.sqlite3"))
        self.max_attempts = max(1, max_attempts)
        self._db_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._last_sync = 0.0
        if not self.db_path.exists():
            self.store.restore_database(self.db_path)

    def connect(self):
        return _SQLiteSession(self.db_path, self._db_lock, self)

    def _maybe_sync_snapshot(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_sync < 30:
            return
        if not self._sync_lock.acquire(blocking=False):
            return
        if not force and time.monotonic() - self._last_sync < 30:
            self._sync_lock.release()
            return
        self._db_lock.acquire()
        try:
            self.store.save_database(self.db_path)
            self._last_sync = time.monotonic()
        except Exception:
            # The local transaction is already committed. Keep the queue live and retry
            # on the next mutation/forced flush; completed records are also written separately.
            log.exception("Could not synchronize SQLite snapshot to the GitHub data repository")
        finally:
            # Release the sync lock before the DB lock so the next writer can immediately
            # schedule a follow-up snapshot after its transaction commits.
            self._sync_lock.release()
            self._db_lock.release()

    def flush_snapshot(self) -> None:
        """Force a durable GitHub snapshot after critical events or at shutdown."""
        self._maybe_sync_snapshot(force=True)

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_bot_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL UNIQUE,
                    post_url TEXT NOT NULL,
                    quality INTEGER,
                    status TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','downloading','ready','uploading','completed','failed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    priority INTEGER NOT NULL DEFAULT 100,
                    requested_by INTEGER,
                    status_chat_id INTEGER,
                    status_message_id INTEGER,
                    output_path TEXT,
                    last_error TEXT,
                    worker_id TEXT,
                    lease_until TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    batch_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    completed_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_queue_idx
                ON tmjdlc_bot_jobs(priority, created_at) WHERE status='queued'
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_batch_idx
                ON tmjdlc_bot_jobs(batch_id, status) WHERE batch_id IS NOT NULL
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_lease_idx
                ON tmjdlc_bot_jobs(lease_until) WHERE status IN ('downloading','uploading')
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_job_stages (
                    job_id INTEGER NOT NULL REFERENCES tmjdlc_bot_jobs(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result JSONB NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(job_id, stage)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER REFERENCES tmjdlc_bot_jobs(id) ON DELETE CASCADE,
                    event TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_media (
                    video_id TEXT PRIMARY KEY,
                    job_id INTEGER REFERENCES tmjdlc_bot_jobs(id),
                    file_path TEXT,
                    file_size INTEGER,
                    duration_seconds REAL,
                    width INTEGER,
                    height INTEGER,
                    video_chat_id INTEGER,
                    video_message_id INTEGER,
                    metadata JSONB NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_workers (
                    worker_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'online',
                    last_heartbeat TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    details JSONB NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_upload_records (
                    job_id INTEGER PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    width INTEGER,
                    height INTEGER,
                    upload_destination TEXT NOT NULL,
                    telegram_chat_id INTEGER,
                    telegram_message_id INTEGER,
                    telegram_file_id TEXT,
                    gofile_file_id TEXT,
                    gofile_download_url TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}',
                    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
    def enqueue(self, video_id: str, post_url: str, quality: int | None = None, *,
                requested_by: int | None = None, status_chat_id: int | None = None,
                status_message_id: int | None = None, priority: int = 100) -> tuple[dict[str, Any], bool]:
        with self.connect() as conn:
            row = conn.execute("""
                INSERT INTO tmjdlc_bot_jobs
                  (video_id,post_url,quality,requested_by,status_chat_id,status_message_id,
                   priority,max_attempts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(video_id) DO NOTHING RETURNING *
            """, (video_id, post_url, quality, requested_by, status_chat_id,
                  status_message_id, priority, self.max_attempts)).fetchone()
            if row:
                result = (dict(row), True)
            else:
                existing = conn.execute(
                    "SELECT * FROM tmjdlc_bot_jobs WHERE video_id=%s", (video_id,)
                ).fetchone()
                if existing and existing["status"] in {"failed", "cancelled"}:
                    row = conn.execute("""
                        UPDATE tmjdlc_bot_jobs SET post_url=%s,quality=%s,status='queued',
                          attempts=0,last_error=NULL,worker_id=NULL,lease_until=NULL,
                          cancel_requested=FALSE,current_stage=NULL,progress_percent=0,
                          requested_by=%s,status_chat_id=%s,status_message_id=%s,
                          priority=%s,updated_at=now()
                        WHERE video_id=%s RETURNING *
                    """, (post_url, quality, requested_by, status_chat_id, status_message_id,
                          priority, video_id)).fetchone()
                    conn.execute("DELETE FROM tmjdlc_job_stages WHERE job_id=%s", (row["id"],))
                    result = (dict(row), True)
                else:
                    result = (dict(existing), False)
        self.flush_snapshot()
        return result

    def set_status_message(self, job_id: int, chat_id: int, message_id: int) -> bool:
        """Persist the dedicated Telegram progress message for one job."""
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs
                SET status_chat_id=%s,status_message_id=%s,updated_at=now()
                WHERE id=%s RETURNING id
            """, (chat_id, message_id, job_id)).fetchone()
            return row is not None

    def enqueue_many(self, items: list[tuple[str, str, int | None, int | None]], *,
                     skip_existing: bool = True, batch_id: str | None = None,
                     status_chat_id: int | None = None,
                     status_message_id: int | None = None) -> tuple[int, int]:
        added = skipped = 0
        with self.connect() as conn:
            for video_id, post_url, quality, requested_by in items:
                row = conn.execute("""
                    INSERT INTO tmjdlc_bot_jobs
                      (video_id,post_url,quality,requested_by,priority,max_attempts,
                       batch_id,status_chat_id,status_message_id)
                    VALUES (%s,%s,%s,%s,100,%s,%s,%s,%s)
                    ON CONFLICT(video_id) DO NOTHING RETURNING id
                """, (video_id, post_url, quality, requested_by,
                      self.max_attempts, batch_id,
                      status_chat_id, status_message_id)).fetchone()
                if row:
                    added += 1
                    continue
                old = conn.execute(
                    "SELECT id,status FROM tmjdlc_bot_jobs WHERE video_id=%s", (video_id,)
                ).fetchone()
                if not skip_existing and old["status"] in {"failed", "cancelled"}:
                    conn.execute("""
                        UPDATE tmjdlc_bot_jobs SET status='queued',attempts=0,last_error=NULL,
                          worker_id=NULL,lease_until=NULL,cancel_requested=FALSE,
                          current_stage=NULL,progress_percent=0,post_url=%s,quality=%s,
                          requested_by=%s,batch_id=%s,
                          status_chat_id=%s,status_message_id=%s,updated_at=now()
                        WHERE id=%s
                    """, (post_url, quality, requested_by, batch_id,
                          status_chat_id, status_message_id, old["id"]))
                    conn.execute("DELETE FROM tmjdlc_job_stages WHERE job_id=%s", (old["id"],))
                    added += 1
                else:
                    skipped += 1
        self.flush_snapshot()
        return added, skipped

    def claim(self, worker_id: str, lease_seconds: int = 900, *,
              max_downloading: int = 1, max_uploads: int = 1,
              max_pending_uploads: int = 1,
              background_download: bool = False) -> dict[str, Any] | None:
        """Claim a download while both download and upload backlogs have capacity.

        Multiple download workers may run concurrently, bounded by max_downloading.
        Active uploads and waiting-ready jobs have separate limits. When
        background_download is enabled, downloads may continue while every upload
        slot is busy, but the ready backlog remains bounded.
        """
        download_limit = max(1, int(max_downloading))
        upload_limit = max(1, int(max_uploads))
        # The waiting queue is separate from active upload slots. For one uploader
        # and a backlog of one, the next download stops as soon as one video is ready.
        upload_backlog_limit = max(1, upload_limit * max(0, int(max_pending_uploads)))
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734821)")
            # Quarantine malformed legacy rows instead of letting them occupy a
            # download slot or repeatedly re-enter the runnable queue.
            conn.execute("""
                UPDATE tmjdlc_bot_jobs
                SET status='failed',worker_id=NULL,lease_until=NULL,
                    last_error='Invalid or missing Javtiful source URL; removed from runnable queue',
                    updated_at=now()
                WHERE status='queued'
                  AND (TRIM(COALESCE(video_id,''))=''
                    OR NOT (
                      LOWER(TRIM(COALESCE(post_url,''))) LIKE 'https://javtiful.com/%'
                      OR LOWER(TRIM(COALESCE(post_url,''))) LIKE 'https://www.javtiful.com/%'
                      OR LOWER(TRIM(COALESCE(post_url,''))) LIKE 'http://javtiful.com/%'
                      OR LOWER(TRIM(COALESCE(post_url,''))) LIKE 'http://www.javtiful.com/%'
                    ))
            """)
            row = conn.execute("""
                WITH candidate AS (
                  SELECT id FROM tmjdlc_bot_jobs
                  WHERE status='queued' AND attempts < max_attempts AND cancel_requested=FALSE
                    AND TRIM(COALESCE(video_id,'')) <> ''
                    AND (LOWER(TRIM(COALESCE(post_url,''))) LIKE 'https://javtiful.com/%'
                         OR LOWER(TRIM(COALESCE(post_url,''))) LIKE 'https://www.javtiful.com/%'
                         OR LOWER(TRIM(COALESCE(post_url,''))) LIKE 'http://javtiful.com/%'
                         OR LOWER(TRIM(COALESCE(post_url,''))) LIKE 'http://www.javtiful.com/%')
                    AND (SELECT count(*) FROM tmjdlc_bot_jobs WHERE status='downloading') < %s
                    AND (%s=TRUE OR
                         (SELECT count(*) FROM tmjdlc_bot_jobs WHERE status='uploading') < %s)
                    AND (SELECT count(*) FROM tmjdlc_bot_jobs WHERE status='ready') < %s
                  ORDER BY priority ASC, created_at ASC, id ASC
                  LIMIT 1
                )
                UPDATE tmjdlc_bot_jobs j SET status='downloading',
                  attempts=j.attempts+1,worker_id=%s,
                  lease_until=now()+(%s*interval '1 second'),
                  started_at=COALESCE(j.started_at,now()),updated_at=now()
                FROM candidate c WHERE j.id=c.id RETURNING j.*
            """, (download_limit, bool(background_download), upload_limit,
                  upload_backlog_limit, worker_id, lease_seconds)).fetchone()
            if row:
                conn.execute("""
                    INSERT INTO tmjdlc_workers(worker_id,status,last_heartbeat)
                    VALUES(%s,'online',now()) ON CONFLICT(worker_id) DO UPDATE
                    SET status='online',last_heartbeat=now()
                """, (worker_id,))
            return dict(row) if row else None

    def claim_upload(self, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
        """Claim the oldest ready video for one of the concurrent upload workers."""
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734821)")
            row = conn.execute("""
                WITH candidate AS (
                  SELECT id FROM tmjdlc_bot_jobs
                  WHERE status='ready' AND cancel_requested=FALSE
                  ORDER BY priority ASC, created_at ASC, id ASC
                  LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                UPDATE tmjdlc_bot_jobs j SET status='uploading',worker_id=%s,
                  lease_until=now()+(%s*interval '1 second'),updated_at=now()
                FROM candidate c WHERE j.id=c.id RETURNING j.*
            """, (worker_id, lease_seconds)).fetchone()
            if row:
                conn.execute("""
                    INSERT INTO tmjdlc_workers(worker_id,status,last_heartbeat)
                    VALUES(%s,'online',now()) ON CONFLICT(worker_id) DO UPDATE
                    SET status='online',last_heartbeat=now()
                """, (worker_id,))
            return dict(row) if row else None

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int = 900) -> bool:
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET lease_until=now()+(%s*interval '1 second'),
                  updated_at=now()
                WHERE id=%s AND worker_id=%s AND status IN ('downloading','uploading')
                  AND cancel_requested=FALSE AND lease_until>now()
                RETURNING id
            """, (lease_seconds, job_id, worker_id)).fetchone()
            if row:
                conn.execute("""
                    INSERT INTO tmjdlc_workers(worker_id,status,last_heartbeat)
                    VALUES(%s,'online',now()) ON CONFLICT(worker_id) DO UPDATE
                    SET status='online',last_heartbeat=now()
                """, (worker_id,))
            return row is not None

    def set_status(self, job_id: int, worker_id: str, status: str, *,
                   output_path: str | None = None, error: str | None = None,
                   lease_seconds: int = 900) -> bool:
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET status=%s,
                  output_path=COALESCE(%s,output_path),last_error=%s,updated_at=now(),
                  lease_until=CASE WHEN %s IN ('downloading','uploading')
                    THEN now()+(%s*interval '1 second') ELSE NULL END,
                  completed_at=CASE WHEN %s='completed' THEN now() ELSE completed_at END,
                  worker_id=CASE WHEN %s IN ('completed','failed','cancelled','queued','ready')
                    THEN NULL ELSE worker_id END
                WHERE id=%s AND worker_id=%s AND lease_until>now()
                RETURNING id
            """, (status, output_path, error, status, lease_seconds, status, status,
                  job_id, worker_id)).fetchone()
            changed = row is not None
        if changed and status in {"ready", "completed", "failed", "cancelled"}:
            self.flush_snapshot()
        return changed

    def request_cancel(self, job_id: int) -> str | None:
        """Immediately cancel queued jobs; request cooperative cancellation for active jobs."""
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET
                  status=CASE WHEN status IN ('queued','ready','failed') THEN 'cancelled' ELSE status END,
                  cancel_requested=TRUE,
                  worker_id=CASE WHEN status IN ('queued','ready','failed') THEN NULL ELSE worker_id END,
                  lease_until=CASE WHEN status IN ('queued','ready','failed') THEN NULL ELSE lease_until END,
                  updated_at=now()
                WHERE id=%s AND status IN ('queued','ready','failed','downloading','uploading')
                RETURNING status
            """, (job_id,)).fetchone()
            return row["status"] if row else None

    def cancel_batch(self, batch_id: str) -> dict[str, int]:
        """Cancel all unfinished jobs created by one batch; active jobs stop cooperatively."""
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET
                  status=CASE WHEN status IN ('queued','ready') THEN 'cancelled' ELSE status END,
                  cancel_requested=TRUE,
                  worker_id=CASE WHEN status IN ('queued','ready') THEN NULL ELSE worker_id END,
                  lease_until=CASE WHEN status IN ('queued','ready') THEN NULL ELSE lease_until END,
                  updated_at=now()
                WHERE batch_id=%s AND status IN ('queued','ready','downloading','uploading')
                RETURNING status
            """, (batch_id,)).fetchall()
            counts = {"cancelled": 0, "stopping": 0}
            for row in rows:
                if row["status"] == "cancelled":
                    counts["cancelled"] += 1
                else:
                    counts["stopping"] += 1
            return counts

    def is_cancel_requested(self, job_id: int, worker_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT cancel_requested OR worker_id IS DISTINCT FROM %s AS stop
                FROM tmjdlc_bot_jobs WHERE id=%s
            """, (worker_id, job_id)).fetchone()
            return bool(row and row["stop"])

    def update_progress(self, job_id: int, worker_id: str, stage: str, percent: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET current_stage=%s,
                  progress_percent=%s,updated_at=now()
                WHERE id=%s AND worker_id=%s AND lease_until>now()
                RETURNING id
            """, (stage, max(0, min(100, percent)), job_id, worker_id)).fetchone()
            return row is not None

    def stage_done(self, job_id: int, stage: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT result FROM tmjdlc_job_stages
                WHERE job_id=%s AND stage=%s AND status='completed'
            """, (job_id, stage)).fetchone()
            if not row:
                return None
            result = row["result"]
            # SQLite stores JSON columns as text; PostgreSQL drivers may return
            # an already-decoded mapping. Normalize both representations.
            if isinstance(result, (bytes, bytearray)):
                result = result.decode("utf-8")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (TypeError, ValueError):
                    log.warning("Invalid JSON stage result | job_id=%s stage=%s",
                                job_id, stage)
                    return None
            return dict(result) if isinstance(result, dict) else None

    def complete_stage(self, job_id: int, worker_id: str, stage: str,
                       result: dict[str, Any] | None = None) -> bool:
        import json
        with self.connect() as conn:
            owned = conn.execute("""
                SELECT id FROM tmjdlc_bot_jobs
                WHERE id=%s AND worker_id=%s AND lease_until>now()
                  AND cancel_requested=FALSE
            """, (job_id, worker_id)).fetchone()
            if not owned:
                return False
            conn.execute("""
                INSERT INTO tmjdlc_job_stages(job_id,stage,status,result,updated_at)
                VALUES(%s,%s,'completed',%s::jsonb,now())
                ON CONFLICT(job_id,stage) DO UPDATE
                SET status='completed',result=EXCLUDED.result,updated_at=now()
            """, (job_id, stage, json.dumps(result or {})))
            conn.execute("""
                INSERT INTO tmjdlc_job_events(job_id,event,details)
                VALUES(%s,'stage_completed',%s::jsonb)
            """, (job_id, json.dumps({"stage": stage})))
            return True

    def recover_orphaned_jobs(self, stale_after_seconds: int = 90) -> int:
        """Fail active jobs whose owning worker has stopped heartbeating.

        This prevents abandoned DB leases from appearing as live downloads/uploads
        or consuming worker capacity after a Colab/runtime restart.
        """
        stale_after_seconds = max(60, int(stale_after_seconds))
        with self.connect() as conn:
            # This queue runs on SQLite (not PostgreSQL). Keep this recovery
            # statement SQLite-native instead of passing PostgreSQL interval,
            # boolean, and UPDATE-alias syntax through the generic translator.
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs
                SET status='failed',worker_id=NULL,lease_until=NULL,
                    cancel_requested=1,
                    last_error='Orphaned task: owning worker heartbeat is no longer active',
                    updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('downloading','uploading')
                  AND NOT EXISTS (
                    SELECT 1 FROM tmjdlc_workers w
                    WHERE w.worker_id=tmjdlc_bot_jobs.worker_id
                      AND w.status='online'
                      AND w.last_heartbeat > datetime(
                          'now', printf('-%d seconds', ?)
                      )
                  )
                RETURNING id
            """, (stale_after_seconds,)).fetchall()
            return len(rows)

    def recover_expired(self) -> int:
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET
                  status=CASE WHEN cancel_requested THEN 'cancelled'
                    WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                  worker_id=NULL,lease_until=NULL,
                  last_error=COALESCE(last_error,'Worker lease expired; job recovered'),
                  updated_at=now()
                WHERE status IN ('downloading','uploading') AND lease_until<now()
                RETURNING id
            """).fetchall()
            return len(rows)

    def save_media(self, video_id: str, job_id: int, file_path: str,
                   file_size: int, duration_seconds: float,
                   width: int | None = None, height: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO tmjdlc_media
                  (video_id,job_id,file_path,file_size,duration_seconds,width,height,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT(video_id) DO UPDATE SET
                  job_id=EXCLUDED.job_id,file_path=EXCLUDED.file_path,
                  file_size=EXCLUDED.file_size,duration_seconds=EXCLUDED.duration_seconds,
                  width=EXCLUDED.width,height=EXCLUDED.height,updated_at=now()
            """, (video_id, job_id, file_path, file_size, duration_seconds, width, height))

    def save_upload_record(self, record: dict[str, Any]) -> str:
        """Persist complete delivery metadata locally and as an individual GitHub JSON record."""
        payload = dict(record)
        telegram = payload.get("telegram") or {}
        parts = telegram.get("parts") or []
        last_part = parts[-1] if parts else {}
        gofile = payload.get("gofile") or {}
        metadata = json.dumps(payload, ensure_ascii=False, default=str)
        job_id = int(payload["job_id"])
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO tmjdlc_upload_records (
                    job_id,video_id,source_url,filename,file_size,duration_seconds,
                    width,height,upload_destination,telegram_chat_id,
                    telegram_message_id,telegram_file_id,gofile_file_id,
                    gofile_download_url,metadata,uploaded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(job_id) DO UPDATE SET
                    video_id=excluded.video_id,source_url=excluded.source_url,
                    filename=excluded.filename,file_size=excluded.file_size,
                    duration_seconds=excluded.duration_seconds,width=excluded.width,
                    height=excluded.height,upload_destination=excluded.upload_destination,
                    telegram_chat_id=excluded.telegram_chat_id,
                    telegram_message_id=excluded.telegram_message_id,
                    telegram_file_id=excluded.telegram_file_id,
                    gofile_file_id=excluded.gofile_file_id,
                    gofile_download_url=excluded.gofile_download_url,
                    metadata=excluded.metadata,uploaded_at=excluded.uploaded_at
            """, (
                job_id, str(payload.get("video_id") or ""),
                str(payload.get("source_url") or ""), str(payload.get("filename") or ""),
                int(payload.get("file_size") or 0), float(payload.get("duration_seconds") or 0),
                payload.get("width"), payload.get("height"),
                str(payload.get("upload_destination") or "telegram"),
                last_part.get("chat_id") or telegram.get("chat_id"),
                last_part.get("message_id") or telegram.get("message_id"),
                last_part.get("file_id"), gofile.get("id") or gofile.get("file_id"),
                gofile.get("download_url"), metadata, payload.get("uploaded_at"),
            ))
        path = self.store.save_upload_record(payload)
        self.flush_snapshot()
        return path

    def clear_media_path(self, video_id: str) -> None:
        """Forget the temporary local path after its media has been uploaded and cleaned."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE tmjdlc_media SET file_path=NULL,updated_at=now() WHERE video_id=%s",
                (video_id,),
            )

    def get(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tmjdlc_bot_jobs WHERE id=%s", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_by_video(self, video_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tmjdlc_bot_jobs WHERE video_id=%s", (video_id,)).fetchone()
            return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status,count(*) AS total FROM tmjdlc_bot_jobs GROUP BY status").fetchall()
            return {row["status"]: row["total"] for row in rows}

    def list_jobs(self, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        """List only work that can still run; completed/failed/cancelled history is not queue UI."""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id,video_id,post_url,quality,status,attempts,max_attempts,
                       last_error,created_at,current_stage,progress_percent
                FROM tmjdlc_bot_jobs
                WHERE status IN ('queued','downloading','ready','uploading')
                ORDER BY CASE status
                    WHEN 'downloading' THEN 0 WHEN 'uploading' THEN 1
                    WHEN 'ready' THEN 2 ELSE 3 END,
                    created_at DESC,id DESC
                LIMIT %s OFFSET %s
            """, (max(1, min(50, limit)), max(0, offset))).fetchall()
            return [dict(row) for row in rows]

    def list_active_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return live download, ready-to-upload and upload jobs for the process monitor."""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id,video_id,post_url,status,worker_id,current_stage,
                       progress_percent,attempts,max_attempts,created_at,updated_at,
                       lease_until,output_path
                FROM tmjdlc_bot_jobs
                WHERE status IN ('downloading','ready','uploading')
                ORDER BY CASE status
                    WHEN 'uploading' THEN 0 WHEN 'downloading' THEN 1 ELSE 2 END,
                    updated_at DESC,id ASC
                LIMIT %s
            """, (max(1, min(50, int(limit))),)).fetchall()
            return [dict(row) for row in rows]

    def cancel_queued(self) -> int:
        """Cancel every job that has not started; active jobs are left untouched."""
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET status='cancelled',cancel_requested=TRUE,
                    worker_id=NULL,lease_until=NULL,updated_at=now()
                WHERE status IN ('queued','ready') RETURNING id
            """).fetchall()
            return len(rows)

    def cancel_all(self) -> dict[str, int]:
        """Cancel every unfinished job; active workers stop cooperatively."""
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET
                  status=CASE WHEN status IN ('queued','ready') THEN 'cancelled' ELSE status END,
                  cancel_requested=TRUE,
                  worker_id=CASE WHEN status IN ('queued','ready') THEN NULL ELSE worker_id END,
                  lease_until=CASE WHEN status IN ('queued','ready') THEN NULL ELSE lease_until END,
                  updated_at=now()
                WHERE status IN ('queued','ready','downloading','uploading')
                RETURNING status
            """).fetchall()
            counts = {"cancelled": 0, "stopping": 0}
            for row in rows:
                if row["status"] == "cancelled":
                    counts["cancelled"] += 1
                else:
                    counts["stopping"] += 1
            return counts

    def clear_completed_history(self) -> int:
        """Remove finished queue history while preserving media catalogue records."""
        with self.connect() as conn:
            # The media catalogue intentionally survives queue-history cleanup.
            # Its job_id FK is restrictive, so detach references to failed/cancelled
            # jobs before deleting those queue rows.
            conn.execute("""
                UPDATE tmjdlc_media
                SET job_id=NULL
                WHERE job_id IN (
                    SELECT id FROM tmjdlc_bot_jobs
                    WHERE status IN ('failed','cancelled')
                )
            """)
            rows = conn.execute("""
                DELETE FROM tmjdlc_bot_jobs
                WHERE status IN ('failed','cancelled')
                   OR (status='completed'
                       AND NOT EXISTS (
                           SELECT 1 FROM tmjdlc_media m
                           WHERE m.job_id=tmjdlc_bot_jobs.id
                       ))
                RETURNING id
            """).fetchall()
            return len(rows)

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM tmjdlc_bot_settings WHERE key=%s", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO tmjdlc_bot_settings(key,value) VALUES(%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()
            """, (key, value))
