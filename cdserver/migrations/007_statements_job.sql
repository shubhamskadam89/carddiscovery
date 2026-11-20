-- The statement-first job (cdserver.jobs.statements), queued right after the quick scan.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'discover_quick', 'discover_full', 'pdf_enrich', 'daily_sync', 'token_check', 'disconnect', 'statements'));
