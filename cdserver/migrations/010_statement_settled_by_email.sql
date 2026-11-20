-- A statement group can be settled by its email alone, without opening the PDF: the subject
-- names the product and the attachment's file name carries the card's digits (HDFC names its
-- PDFs "0036XXXXXXXXXX13_17-11-2026_544.pdf": first 4 and last 2 digits).
ALTER TABLE statement_pdf_reads DROP CONSTRAINT statement_pdf_reads_outcome_check;
ALTER TABLE statement_pdf_reads ADD CONSTRAINT statement_pdf_reads_outcome_check
    CHECK (outcome IN ('read', 'no_pdf', 'password', 'no_number', 'unreadable', 'email', 'checked'));
-- checked: the email was read but its PDF not opened (no consent to read statement PDFs yet);
-- it is tried again for its PDF once the user consents.
