-- Login (phone + OTP) is handled by a separate service. This service only receives
-- that service's user id, so accounts are keyed by it and the OTP tables go.
DROP TABLE otp_sends;
DROP TABLE otps;
ALTER TABLE accounts RENAME COLUMN phone TO external_id;
