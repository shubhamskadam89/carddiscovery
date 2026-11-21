-- Every correction a user makes, in one place, for the team to track (cdserver/reports.py):
--   source  app      a button in the app ("This card isn't mine", "Not about this card")
--           forward  the user forwarded the bank email to the team's report inbox (CD_REPORT_INBOX)
--   kind    not_my_card | wrong_email | other
--   status  applied  matched to the email we used and acted on (email left out / card marked "no")
--           open     not matched (or not acted on): someone on the team should look
--           fixed    the team has dealt with it
CREATE TABLE user_reports (
    id               bigserial PRIMARY KEY,
    account_id       bigint REFERENCES accounts ON DELETE CASCADE,
    source           text NOT NULL CHECK (source IN ('app', 'forward')),
    kind             text NOT NULL CHECK (kind IN ('not_my_card', 'wrong_email', 'other')),
    status           text NOT NULL DEFAULT 'open' CHECK (status IN ('applied', 'open', 'fixed')),
    message_id       text,             -- the evidence email it's about, when known
    card_key         text,
    bank_id          text,
    last4            text,
    note             text,             -- what the user wrote above the forward
    subject          text,             -- the forwarded bank email's subject (the user chose to send it)
    from_address     text,             -- who forwarded it
    inbox_message_id text UNIQUE,      -- the forward in the report inbox: read once
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_reports_account ON user_reports (account_id, created_at DESC);

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'discover_quick', 'discover_full', 'pdf_enrich', 'daily_sync', 'token_check', 'disconnect', 'statements',
    'llm_fallback', 'forwards', 'report_inbox'));
