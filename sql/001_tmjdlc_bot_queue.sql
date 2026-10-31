-- Additive TMJDLC bot queue schema. Existing project tables are untouched.
CREATE TABLE IF NOT EXISTS public.tmjdlc_bot_jobs (
    id BIGSERIAL PRIMARY KEY,
    video_id TEXT NOT NULL UNIQUE,
    post_url TEXT NOT NULL,
    quality INTEGER,
    upload_screenshots BOOLEAN NOT NULL DEFAULT TRUE,
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
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_queue_idx
    ON public.tmjdlc_bot_jobs(priority, created_at) WHERE status='queued';
CREATE INDEX IF NOT EXISTS tmjdlc_bot_jobs_lease_idx
    ON public.tmjdlc_bot_jobs(lease_until) WHERE status IN ('downloading','uploading');
CREATE TABLE IF NOT EXISTS public.tmjdlc_bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
