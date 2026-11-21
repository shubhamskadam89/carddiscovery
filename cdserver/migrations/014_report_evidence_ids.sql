-- The emails a report is about, by Gmail message id ("<id>#pdf" for a statement PDF's row): every
-- evidence email of the card for "This card isn't mine", the one email for "Not about this card".
-- The team fetches them live with the user's read-only access when reviewing; no email text is stored.
ALTER TABLE user_reports ADD COLUMN evidence_ids text[] NOT NULL DEFAULT '{}';
