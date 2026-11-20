-- Which PDF reader version made each statement PDF attempt (cdserver.pdf.PDF_READER_VERSION).
-- Attempts by an older reader are made once more when the reading improves.
ALTER TABLE statement_pdf_reads ADD COLUMN pdf_version int NOT NULL DEFAULT 1;
