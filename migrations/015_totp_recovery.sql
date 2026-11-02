ALTER TABLE app_users ADD COLUMN pending_totp_secret TEXT;
CREATE TABLE auth_recovery_codes (
 user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
 code_hash CHAR(64) NOT NULL, consumed_at TIMESTAMPTZ,
 PRIMARY KEY(user_id,code_hash)
);
