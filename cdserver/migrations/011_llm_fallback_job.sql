-- The shadow Claude Haiku fallback (cdserver.jobs.llm_fallback), queued after a full scan or a sync
-- for accounts that allow it; it reads the emails the rules couldn't type and changes no card.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'discover_quick', 'discover_full', 'pdf_enrich', 'daily_sync', 'token_check', 'disconnect', 'statements',
    'llm_fallback'));
