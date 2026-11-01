"""Durable PostgreSQL queue with leases, resumable stages and cooperative cancellation."""
from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


class QueueNotConfigured(RuntimeError):
    pass


class JobQueue:
    """Neon-backed queue. A worker may mutate a job only while it owns its lease."""

    def __init__(self, dsn: str | None = None, *, max_attempts: int = 3) -> None:
        self.dsn = (dsn or DATABASE_URL).strip()
        if not self.dsn:
            raise QueueNotConfigured("DATABASE_URL is required for the durable queue.")
        self.max_attempts = max(1, max_attempts)

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=15)

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_bot_jobs (
                    id BIGSERIAL PRIMARY KEY, video_id TEXT NOT NULL UNIQUE,
                    post_url TEXT NOT NULL, quality INTEGER,
                    upload_screenshots BOOLEAN NOT NULL DEFAULT TRUE,
                    status TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','downloading','uploading','completed','failed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                    priority INTEGER NOT NULL DEFAULT 100, requested_by BIGINT,
                    status_chat_id BIGINT, status_message_id BIGINT, output_path TEXT,
                    last_error TEXT, worker_id TEXT, lease_until TIMESTAMPTZ,
                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    current_stage TEXT, progress_percent INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
                )
            """)
            for definition in (
                "upload_screenshots BOOLEAN NOT NULL DEFAULT TRUE",
                "cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
                "current_stage TEXT",
                "progress_percent INTEGER NOT NULL DEFAULT 0",
                "batch_id TEXT",
            ):
                conn.execute(f"ALTER TABLE tmjdlc_bot_jobs ADD COLUMN IF NOT EXISTS {definition}")
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
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_job_stages (
                    job_id BIGINT NOT NULL REFERENCES tmjdlc_bot_jobs(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    result JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY(job_id, stage)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_job_events (
                    id BIGSERIAL PRIMARY KEY,
                    job_id BIGINT REFERENCES tmjdlc_bot_jobs(id) ON DELETE CASCADE,
                    event TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_media (
                    video_id TEXT PRIMARY KEY, job_id BIGINT REFERENCES tmjdlc_bot_jobs(id),
                    file_path TEXT, file_size BIGINT, duration_seconds DOUBLE PRECISION,
                    width INTEGER, height INTEGER, video_chat_id BIGINT,
                    video_message_id BIGINT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_workers (
                    worker_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'online',
                    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
                    details JSONB NOT NULL DEFAULT '{}'::jsonb
                )
            """)

    def enqueue(self, video_id: str, post_url: str, quality: int | None = None, *,
                requested_by: int | None = None, status_chat_id: int | None = None,
                status_message_id: int | None = None, priority: int = 100,
                upload_screenshots: bool = True) -> tuple[dict[str, Any], bool]:
        with self.connect() as conn:
            row = conn.execute("""
                INSERT INTO tmjdlc_bot_jobs
                  (video_id,post_url,quality,requested_by,status_chat_id,status_message_id,
                   priority,max_attempts,upload_screenshots)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(video_id) DO NOTHING RETURNING *
            """, (video_id, post_url, quality, requested_by, status_chat_id,
                  status_message_id, priority, self.max_attempts, upload_screenshots)).fetchone()
            if row:
                return dict(row), True
            existing = conn.execute(
                "SELECT * FROM tmjdlc_bot_jobs WHERE video_id=%s", (video_id,)
            ).fetchone()
            if existing and existing["status"] in {"failed", "cancelled"}:
                row = conn.execute("""
                    UPDATE tmjdlc_bot_jobs SET post_url=%s,quality=%s,status='queued',
                      attempts=0,last_error=NULL,worker_id=NULL,lease_until=NULL,
                      cancel_requested=FALSE,current_stage=NULL,progress_percent=0,
                      requested_by=%s,status_chat_id=%s,status_message_id=%s,
                      priority=%s,upload_screenshots=%s,updated_at=now()
                    WHERE video_id=%s RETURNING *
                """, (post_url, quality, requested_by, status_chat_id, status_message_id,
                      priority, upload_screenshots, video_id)).fetchone()
                conn.execute("DELETE FROM tmjdlc_job_stages WHERE job_id=%s", (row["id"],))
                return dict(row), True
            return dict(existing), False

    def enqueue_many(self, items: list[tuple[str, str, int | None, int | None, bool]], *,
                     skip_existing: bool = True, batch_id: str | None = None) -> tuple[int, int]:
        added = skipped = 0
        with self.connect() as conn:
            for video_id, post_url, quality, requested_by, screenshots in items:
                row = conn.execute("""
                    INSERT INTO tmjdlc_bot_jobs
                      (video_id,post_url,quality,requested_by,priority,max_attempts,upload_screenshots,batch_id)
                    VALUES (%s,%s,%s,%s,100,%s,%s,%s)
                    ON CONFLICT(video_id) DO NOTHING RETURNING id
                """, (video_id, post_url, quality, requested_by,
                      self.max_attempts, screenshots, batch_id)).fetchone()
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
                          requested_by=%s,upload_screenshots=%s,updated_at=now()
                        WHERE id=%s
                    """, (post_url, quality, requested_by, screenshots, old["id"]))
                    conn.execute("DELETE FROM tmjdlc_job_stages WHERE job_id=%s", (old["id"],))
                    added += 1
                else:
                    skipped += 1
        return added, skipped

    def claim(self, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""
                WITH candidate AS (
                  SELECT id FROM tmjdlc_bot_jobs
                  WHERE status='queued' AND attempts < max_attempts AND cancel_requested=FALSE
                  ORDER BY priority,created_at FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE tmjdlc_bot_jobs j SET status='downloading',
                  attempts=j.attempts+1,worker_id=%s,
                  lease_until=now()+(%s*interval '1 second'),
                  started_at=COALESCE(j.started_at,now()),updated_at=now()
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
                  worker_id=CASE WHEN %s IN ('completed','failed','cancelled','queued')
                    THEN NULL ELSE worker_id END
                WHERE id=%s AND worker_id=%s AND lease_until>now()
                RETURNING id
            """, (status, output_path, error, status, lease_seconds, status, status,
                  job_id, worker_id)).fetchone()
            return row is not None

    def request_cancel(self, job_id: int) -> str | None:
        """Immediately cancel queued jobs; request cooperative cancellation for active jobs."""
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET
                  status=CASE WHEN status IN ('queued','failed') THEN 'cancelled' ELSE status END,
                  cancel_requested=TRUE,
                  worker_id=CASE WHEN status IN ('queued','failed') THEN NULL ELSE worker_id END,
                  lease_until=CASE WHEN status IN ('queued','failed') THEN NULL ELSE lease_until END,
                  updated_at=now()
                WHERE id=%s AND status IN ('queued','failed','downloading','uploading')
                RETURNING status
            """, (job_id,)).fetchone()
            return row["status"] if row else None

    def cancel_batch(self, batch_id: str) -> dict[str, int]:
        """Cancel all unfinished jobs created by one batch; active jobs stop cooperatively."""
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET
                  status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                  cancel_requested=TRUE,
                  worker_id=CASE WHEN status='queued' THEN NULL ELSE worker_id END,
                  lease_until=CASE WHEN status='queued' THEN NULL ELSE lease_until END,
                  updated_at=now()
                WHERE batch_id=%s AND status IN ('queued','downloading','uploading')
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
            return dict(row["result"]) if row else None

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
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id,video_id,post_url,quality,status,attempts,max_attempts,
                       upload_screenshots,last_error,created_at,current_stage,progress_percent
                FROM tmjdlc_bot_jobs ORDER BY id DESC LIMIT %s OFFSET %s
            """, (max(1, min(50, limit)), max(0, offset))).fetchall()
            return [dict(row) for row in rows]

    def cancel_queued(self) -> int:
        """Cancel every job that has not started; active jobs are left untouched."""
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET status='cancelled',cancel_requested=TRUE,
                    worker_id=NULL,lease_until=NULL,updated_at=now()
                WHERE status='queued' RETURNING id
            """).fetchall()
            return len(rows)

    def clear_completed_history(self) -> int:
        """Remove completed queue rows that are not referenced by retained media records."""
        with self.connect() as conn:
            rows = conn.execute("""
                DELETE FROM tmjdlc_bot_jobs j
                WHERE j.status='completed'
                  AND NOT EXISTS (SELECT 1 FROM tmjdlc_media m WHERE m.job_id=j.id)
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
