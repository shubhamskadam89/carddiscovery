-- "Report a mistake" on a card: the user picks the right bank and card and types the last 4 digits.
-- Stored with the report (kind 'correction') as {"bank": ..., "product": ..., "last4": ...}; the card
-- shows it as reported until the team deals with it.
ALTER TABLE user_reports DROP CONSTRAINT user_reports_kind_check;
ALTER TABLE user_reports ADD CONSTRAINT user_reports_kind_check
    CHECK (kind IN ('not_my_card', 'wrong_email', 'correction', 'other'));
ALTER TABLE user_reports ADD COLUMN correct jsonb;
