-- Additive reliability migration for existing TMJDLC Neon databases.
ALTER TABLE public.tmjdlc_bot_jobs
  ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS current_stage TEXT,
  ADD COLUMN IF NOT EXISTS progress_percent INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.tmjdlc_job_stages (
  job_id BIGINT NOT NULL REFERENCES public.tmjdlc_bot_jobs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(job_id, stage)
);

CREATE TABLE IF NOT EXISTS public.tmjdlc_job_events (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT REFERENCES public.tmjdlc_bot_jobs(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.tmjdlc_media (
  video_id TEXT PRIMARY KEY,
  job_id BIGINT REFERENCES public.tmjdlc_bot_jobs(id),
  file_path TEXT,
  file_size BIGINT,
  duration_seconds DOUBLE PRECISION,
  width INTEGER,
  height INTEGER,
  video_chat_id BIGINT,
  video_message_id BIGINT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.tmjdlc_workers (
  worker_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'online',
  last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);
