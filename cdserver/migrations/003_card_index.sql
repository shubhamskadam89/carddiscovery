-- What the full scan learned about each card, so later jobs go straight to the right mail:
-- how the bank writes its number, how many emails mention it, and its statement-PDF emails.
CREATE TABLE card_index (
    account_id       bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    bank_id          text NOT NULL,
    last4            text NOT NULL CHECK (last4 ~ '^[0-9]{4,5}$'),
    email_count      int NOT NULL DEFAULT 0,
    number_tokens    text[] NOT NULL DEFAULT '{}',   -- e.g. {XX2846, 4375XXXXXXXX2846}: masked, never a full number
    pdf_query        text,                           -- the Gmail search that finds its statement PDFs
    pdf_message_ids  text[] NOT NULL DEFAULT '{}',   -- newest first; nothing downloaded yet
    indexed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, bank_id, last4)
);
