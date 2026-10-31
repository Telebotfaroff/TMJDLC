"""Durable PostgreSQL job queue for TMJDLC Telegram downloads."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


class QueueNotConfigured(RuntimeError):
    pass


class JobQueue:
    """PostgreSQL-backed queue using SKIP LOCKED claims and expiring worker leases."""

    def __init__(self, dsn: str | None = None, *, max_attempts: int = 3) -> None:
        self.dsn = (dsn or DATABASE_URL).strip()
        if not self.dsn:
            raise QueueNotConfigured(
                "DATABASE_URL is required for the durable queue. Set it to your Neon PostgreSQL connection string."
            )
        self.max_attempts = max(1, max_attempts)

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=15)

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_bot_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    post_url TEXT NOT NULL,
                    quality INTEGER,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','downloading','uploading','completed','failed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    priority INTEGER NOT NULL DEFAULT 100,
                    requested_by BIGINT,
                    status_chat_id BIGINT,
                    status_message_id BIGINT,
                    output_path TEXT,
                    last_error TEXT,
                    worker_id TEXT,
                    lease_until TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    UNIQUE(video_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_queue_idx
                ON tmjdlc_bot_jobs(priority, created_at) WHERE status='queued'
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_lease_idx
                ON tmjdlc_bot_jobs(lease_until) WHERE status IN ('downloading','uploading')
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tmjdlc_bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

    def enqueue(
        self, video_id: str, post_url: str, quality: int | None = None, *,
        requested_by: int | None = None, status_chat_id: int | None = None,
        status_message_id: int | None = None, priority: int = 100,
    ) -> tuple[dict[str, Any], bool]:
        with self.connect() as conn:
            row = conn.execute("""
                INSERT INTO tmjdlc_bot_jobs
                    (video_id,post_url,quality,requested_by,status_chat_id,status_message_id,priority,max_attempts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(video_id) DO NOTHING
                RETURNING *
            """, (video_id, post_url, quality, requested_by, status_chat_id,
                  status_message_id, priority, self.max_attempts)).fetchone()
            if row:
                return dict(row), True
            existing = conn.execute(
                "SELECT * FROM tmjdlc_bot_jobs WHERE video_id=%s", (video_id,)
            ).fetchone()
            if existing and existing["status"] in {"failed", "cancelled"}:
                row = conn.execute("""
                    UPDATE tmjdlc_bot_jobs SET post_url=%s, quality=%s, status='queued',
                        attempts=0, last_error=NULL, output_path=NULL, worker_id=NULL,
                        lease_until=NULL, requested_by=%s, status_chat_id=%s,
                        status_message_id=%s, priority=%s, updated_at=now()
                    WHERE video_id=%s RETURNING *
                """, (post_url, quality, requested_by, status_chat_id,
                      status_message_id, priority, video_id)).fetchone()
                return dict(row), True
            return dict(existing), False

    def claim(self, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""
                WITH candidate AS (
                    SELECT id FROM tmjdlc_bot_jobs
                    WHERE status='queued' AND attempts < max_attempts
                    ORDER BY priority ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE tmjdlc_bot_jobs AS job
                SET status='downloading', attempts=attempts+1, worker_id=%s,
                    lease_until=now() + (%s * interval '1 second'),
                    started_at=COALESCE(started_at,now()), updated_at=now()
                FROM candidate WHERE job.id=candidate.id
                RETURNING job.*
            """, (worker_id, lease_seconds)).fetchone()
            return dict(row) if row else None

    def set_status(self, job_id: int, worker_id: str, status: str, *,
                   output_path: str | None = None, error: str | None = None,
                   lease_seconds: int = 900) -> bool:
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET status=%s,
                    output_path=COALESCE(%s,output_path),
                    last_error=%s, updated_at=now(),
                    lease_until=CASE WHEN %s IN ('downloading','uploading')
                        THEN now() + (%s * interval '1 second') ELSE NULL END,
                    completed_at=CASE WHEN %s='completed' THEN now() ELSE completed_at END
                WHERE id=%s AND worker_id=%s RETURNING id
            """, (status, output_path, error, status, lease_seconds, status,
                  job_id, worker_id)).fetchone()
            return row is not None

    def recover_expired(self) -> int:
        with self.connect() as conn:
            rows = conn.execute("""
                UPDATE tmjdlc_bot_jobs
                SET status=CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                    worker_id=NULL, lease_until=NULL,
                    last_error=COALESCE(last_error,'Worker lease expired; job recovered'),
                    updated_at=now()
                WHERE status IN ('downloading','uploading')
                  AND lease_until < now()
                RETURNING id
            """).fetchall()
            return len(rows)

    def cancel(self, job_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("""
                UPDATE tmjdlc_bot_jobs SET status='cancelled', worker_id=NULL,
                    lease_until=NULL, updated_at=now()
                WHERE id=%s AND status IN ('queued','failed') RETURNING id
            """, (job_id,)).fetchone()
            return row is not None

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
            rows = conn.execute(
                "SELECT status,count(*) AS total FROM tmjdlc_bot_jobs GROUP BY status"
            ).fetchall()
            return {row["status"]: row["total"] for row in rows}

    def list_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id,video_id,status,attempts,last_error,created_at
                FROM tmjdlc_bot_jobs ORDER BY id DESC LIMIT %s
            """, (max(1, min(50, limit)),)).fetchall()
            return [dict(row) for row in rows]

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
