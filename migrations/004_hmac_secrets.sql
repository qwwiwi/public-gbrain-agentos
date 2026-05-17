-- gbrain Hermes HMAC secrets.
--
-- Additive and idempotent. Raw HMAC secrets are never stored here; only
-- sha256(raw_secret_string) is persisted. Operators keep raw secrets in
-- env/secrets files and issue them once via scripts/issue-hmac-secret.py.

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS hmac_secret_sha256 text,
    ADD COLUMN IF NOT EXISTS hmac_secret_comment text,
    ADD COLUMN IF NOT EXISTS hmac_secret_rotated_at timestamptz;

-- M1: validate column types when the migration is replayed against a
-- database where someone previously created the columns with the
-- wrong type. ``ADD COLUMN IF NOT EXISTS`` silently accepts an
-- existing column regardless of its declared type, so we fail loudly
-- here instead of letting HMAC verification mysteriously break.
DO $$
DECLARE
    actual text;
BEGIN
    SELECT data_type INTO actual
        FROM information_schema.columns
        WHERE table_name = 'agent_tokens'
          AND column_name = 'hmac_secret_sha256';
    IF actual IS NOT NULL AND actual NOT IN ('text', 'character varying') THEN
        RAISE EXCEPTION
            'agent_tokens.hmac_secret_sha256 must be text, found %', actual;
    END IF;

    SELECT data_type INTO actual
        FROM information_schema.columns
        WHERE table_name = 'agent_tokens'
          AND column_name = 'hmac_secret_comment';
    IF actual IS NOT NULL AND actual NOT IN ('text', 'character varying') THEN
        RAISE EXCEPTION
            'agent_tokens.hmac_secret_comment must be text, found %', actual;
    END IF;

    SELECT data_type INTO actual
        FROM information_schema.columns
        WHERE table_name = 'agent_tokens'
          AND column_name = 'hmac_secret_rotated_at';
    IF actual IS NOT NULL AND actual NOT IN ('timestamp with time zone', 'timestamptz') THEN
        RAISE EXCEPTION
            'agent_tokens.hmac_secret_rotated_at must be timestamptz, found %', actual;
    END IF;
END
$$;

COMMENT ON COLUMN agent_tokens.hmac_secret_sha256 IS
    'SHA-256 hex digest of the agent HMAC secret. Raw secret is never stored in Postgres.';

COMMENT ON COLUMN agent_tokens.hmac_secret_comment IS
    'Operator note for HMAC secret provenance or rotation context. Must not contain the raw secret.';

COMMENT ON COLUMN agent_tokens.hmac_secret_rotated_at IS
    'Timestamp of the last HMAC secret issue/rotation, independent from Bearer last_rotated.';

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tokens_hmac_secret_sha256_active
    ON agent_tokens (hmac_secret_sha256)
    WHERE hmac_secret_sha256 IS NOT NULL
      AND revoked_at IS NULL;
