-- What users tell us about their cards' evidence (cdserver/reports.py):
--   email_reports  an email we showed under a card that the user says is wrong ("not about this card");
--                  it is left out when the user's cards are rebuilt, and can be undone.
CREATE TABLE email_reports (
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    message_id  text NOT NULL,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, message_id)
);

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'discover_quick', 'discover_full', 'pdf_enrich', 'daily_sync', 'token_check', 'disconnect', 'statements',
    'llm_fallback', 'forwards'));
