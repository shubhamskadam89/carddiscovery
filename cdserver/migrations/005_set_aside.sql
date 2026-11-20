-- Emails about a card we already have that can't say which one (carddiscovery.resolve.SetAside).
-- Rebuilt from evidence together with the cards; holds no email text. Lets us count them per
-- bank and type, and later re-read the ones whose body was never read (used_body = false).
CREATE TABLE set_aside (
    account_id  bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    message_id  text NOT NULL,
    last4       text NOT NULL DEFAULT '',  -- '' when the email has no card number
    bank_id     text NOT NULL,
    type        text NOT NULL,
    reason      text NOT NULL CHECK (reason IN ('several_cards', 'shared_last4')),
    candidates  int NOT NULL,
    used_body   boolean NOT NULL,
    PRIMARY KEY (account_id, message_id, last4)
);
