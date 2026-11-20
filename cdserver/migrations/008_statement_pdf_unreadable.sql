-- A statement PDF the PDF library can't parse is recorded too, so it's never downloaded again.
ALTER TABLE statement_pdf_reads DROP CONSTRAINT statement_pdf_reads_outcome_check;
ALTER TABLE statement_pdf_reads ADD CONSTRAINT statement_pdf_reads_outcome_check
    CHECK (outcome IN ('read', 'no_pdf', 'password', 'no_number', 'unreadable'));
