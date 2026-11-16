-- Mini version schema (docs/architecture.md §6). No email text is stored anywhere.

CREATE TABLE accounts (
    id          bigserial PRIMARY KEY,
    phone       text UNIQUE NOT NULL,
    deleting    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE otps (
    phone       text PRIMARY KEY,
    code_hmac   text NOT NULL,
    expires_at  timestamptz NOT NULL,
    attempts    int NOT NULL DEFAULT 0,
    sent_at     timestamptz NOT NULL
);
CREATE TABLE otp_sends (phone text NOT NULL, sent_at timestamptz NOT NULL);
CREATE INDEX otp_sends_phone ON otp_sends (phone, sent_at);

CREATE TABLE sessions (
    token_hash  text PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL
);
CREATE INDEX sessions_account ON sessions (account_id);

-- The refresh token is envelope-encrypted (vault.py); only the worker role can open it.
CREATE TABLE gmail_connections (
    account_id        bigint PRIMARY KEY REFERENCES accounts ON DELETE CASCADE,
    mailbox           text NOT NULL,
    token_blob        bytea NOT NULL,
    scopes            text[] NOT NULL,
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'needs_reconnect', 'disconnecting')),
    years             int NOT NULL CHECK (years IN (1, 3, 5)),
    names             text[] NOT NULL DEFAULT '{}',
    pdf_opt_in        boolean NOT NULL DEFAULT false,
    dob_blob          bytea,
    last_synced_at    timestamptz,
    cancel_requested  boolean NOT NULL DEFAULT false,
    connected_at      timestamptz NOT NULL DEFAULT now()
);

-- Derived facts per email (type, bank, last 4, product, date, flags). Lets every
-- later job skip mail already read.
CREATE TABLE evidence (
    account_id         bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    message_id         text NOT NULL,
    extractor_version  text NOT NULL,
    data               jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, message_id)
);

CREATE TABLE decisions (
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    card_key    text NOT NULL,
    decision    text NOT NULL CHECK (decision IN ('yes', 'no')),
    decided_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, card_key)
);

-- source = manual (card picker) or gmail (found in mail; Limited Use rules apply).
CREATE TABLE cards (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    source      text NOT NULL CHECK (source IN ('manual', 'gmail')),
    card_key    text NOT NULL,
    bank_id     text NOT NULL,
    last4       text CHECK (last4 ~ '^[0-9]{4,5}$'),
    product     text,
    network     text,
    grp         text NOT NULL,
    view        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, source, card_key)
);

CREATE TABLE statement_facts (
    id               bigserial PRIMARY KEY,
    account_id       bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    message_id       text NOT NULL,
    bank_id          text NOT NULL,
    last4            text,
    statement_date   date,
    total_due        numeric(14, 2),
    minimum_due      numeric(14, 2),
    credit_limit     numeric(14, 2),
    available_limit  numeric(14, 2),
    reward_points    numeric(14, 2),
    reconciled       boolean,           -- null: the statement has no lines to check
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, message_id)
);

CREATE TABLE notifications (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    kind        text NOT NULL,     -- new_card | reconnect | pdf_password
    detail      jsonb NOT NULL DEFAULT '{}',
    seen        boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Job queue. Rows carry ids and counters, never email content.
CREATE TABLE jobs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('discover_quick', 'discover_full', 'pdf_enrich',
                                               'daily_sync', 'token_check', 'disconnect')),
    account_id   bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    priority     int NOT NULL,
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    payload      jsonb NOT NULL DEFAULT '{}',
    progress     jsonb NOT NULL DEFAULT '{}',
    run_after    timestamptz NOT NULL DEFAULT now(),
    attempts     int NOT NULL DEFAULT 0,
    last_error   text,
    locked_by    text,
    locked_at    timestamptz,
    units_used   int NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz
);
-- One Gmail job per user at a time, so the per-user quota is spent by one job.
CREATE UNIQUE INDEX jobs_one_running_per_account ON jobs (account_id) WHERE status = 'running';
-- Enqueueing the same kind twice is a no-op.
CREATE UNIQUE INDEX jobs_one_queued_per_kind ON jobs (account_id, kind) WHERE status = 'queued';
CREATE INDEX jobs_ready ON jobs (priority, run_after) WHERE status = 'queued';
CREATE INDEX jobs_account ON jobs (account_id, created_at DESC);

CREATE TABLE quota_daily (
    day    date PRIMARY KEY,
    units  bigint NOT NULL DEFAULT 0
);

-- Every token decrypt is recorded (who, when, why).
CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    account_id  bigint,
    actor       text NOT NULL,
    action      text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
