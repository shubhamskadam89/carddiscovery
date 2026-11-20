-- Gmail units spent per user in the last minutes, shared by every job and worker, so the
-- per-user limit (6,000 units a minute at Google) is respected across jobs, not just within one.
CREATE TABLE gmail_usage (
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    at          timestamptz NOT NULL DEFAULT now(),
    units       int NOT NULL
);
CREATE INDEX gmail_usage_recent ON gmail_usage (account_id, at);
