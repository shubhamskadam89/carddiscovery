-- Statement PDFs already tried by the statement-first step (cdserver.jobs.read_statement_gaps), so
-- none is downloaded twice: read (its card number and product are in `evidence` as "<id>#pdf"),
-- no_pdf (no PDF attached), password (couldn't be opened), no_number (opened, no card number).
-- Holds no statement content; deleted on disconnect.
CREATE TABLE statement_pdf_reads (
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    message_id  text NOT NULL,
    bank_id     text NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN ('read', 'no_pdf', 'password', 'no_number')),
    read_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, message_id)
);
