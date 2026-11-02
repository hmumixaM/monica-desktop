CREATE TABLE message_source_cursors (
 source_file TEXT NOT NULL, source_table TEXT NOT NULL, last_local_id BIGINT NOT NULL DEFAULT 0,
 source_count BIGINT NOT NULL DEFAULT 0, projected_count BIGINT NOT NULL DEFAULT 0,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(source_file,source_table)
);
CREATE TABLE message_search (
 message_id UUID NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
 conversation_id UUID NOT NULL REFERENCES conversations(id), body TEXT NOT NULL,
 PRIMARY KEY(message_id,occurred_at)
);
CREATE INDEX message_search_conversation ON message_search(conversation_id,occurred_at DESC,message_id DESC);
CREATE INDEX message_search_body ON message_search USING gin(body gin_trgm_ops);
