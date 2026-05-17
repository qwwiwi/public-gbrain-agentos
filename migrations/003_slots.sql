-- gbrain P0 retrieval pack: per-agent slot scratchpad table.
--
-- Slots are an additive Postgres-only scratchpad scoped by authenticated
-- agent. They are NOT canonical memory: they are not Markdown-backed, not
-- chunked, not embedded, and not indexed by recall.
--
-- Constraints:
--   - label matches ^[a-z][a-z0-9_]{0,63}$
--   - size_limit > 0
--   - hard_cap > 0 AND hard_cap <= 20000
--   - size_limit <= hard_cap
--   - octet_length(content) <= size_limit  (UTF-8 bytes, not characters)
--   - UNIQUE (agent, label)
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS slots (
    id bigserial PRIMARY KEY,
    label text NOT NULL,
    content text NOT NULL DEFAULT '',
    size_limit integer NOT NULL DEFAULT 2000,
    hard_cap integer NOT NULL DEFAULT 20000,
    pinned boolean NOT NULL DEFAULT false,
    agent text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT slots_label_format CHECK (label ~ '^[a-z][a-z0-9_]{0,63}$'),
    CONSTRAINT slots_size_limit_positive CHECK (size_limit > 0),
    CONSTRAINT slots_hard_cap_positive CHECK (hard_cap > 0 AND hard_cap <= 20000),
    CONSTRAINT slots_size_limit_le_hard_cap CHECK (size_limit <= hard_cap),
    CONSTRAINT slots_content_within_size_limit CHECK (octet_length(content) <= size_limit),
    CONSTRAINT slots_agent_label_unique UNIQUE (agent, label)
);

CREATE INDEX IF NOT EXISTS idx_slots_agent_pinned_label
    ON slots(agent, pinned DESC, label);

CREATE INDEX IF NOT EXISTS idx_slots_agent_updated
    ON slots(agent, updated_at DESC);

GRANT ALL PRIVILEGES ON TABLE slots TO gbrain;
GRANT ALL PRIVILEGES ON SEQUENCE slots_id_seq TO gbrain;
