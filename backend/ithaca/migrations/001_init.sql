-- Ithaca schema v1. One SQLite file holds everything: chats, the run event
-- log, documents, memory (text + vectors), settings and secrets.

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);

CREATE TABLE auth_sessions (
    token      TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE providers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('openai', 'anthropic')),
    base_url      TEXT NOT NULL DEFAULT '',
    api_key       TEXT NOT NULL DEFAULT '',
    -- [{"id": "...", "tools": true|false|null, "context": 8192}]
    models        TEXT NOT NULL DEFAULT '[]',
    default_model TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL
);

CREATE TABLE conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'New chat',
    provider_id TEXT,
    model       TEXT,
    document_id TEXT,              -- optional attached document
    pinned      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- Append-only event log. Conversation history is reconstructed by replaying
-- these rows; nothing is ever edited in place.
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    run_id          TEXT,
    type            TEXT NOT NULL,
    data            TEXT NOT NULL,  -- JSON
    created_at      REAL NOT NULL
);
CREATE INDEX events_conv ON events (conversation_id, id);
CREATE INDEX events_run ON events (run_id, id);

CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    status          TEXT NOT NULL,  -- running | waiting_approval | done | error | cancelled
    tainted         INTEGER NOT NULL DEFAULT 0,
    taint_sources   TEXT NOT NULL DEFAULT '[]',
    usage           TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL,
    finished_at     REAL
);

CREATE TABLE approvals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool         TEXT NOT NULL,
    args         TEXT NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied
    created_at   REAL NOT NULL,
    decided_at   REAL
);

CREATE TABLE documents (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE VIRTUAL TABLE documents_fts USING fts5 (title, content, content='documents', content_rowid='rowid', tokenize='porter unicode61');
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts (rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
END;
CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO documents_fts (rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE TABLE memories (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',
    -- 1 when written during a run that had already read untrusted content.
    -- Such memories are replayed to the model as untrusted data.
    tainted    INTEGER NOT NULL DEFAULT 0,
    embedding  BLOB,
    embed_model TEXT,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE memories_fts USING fts5 (content, content='memories', content_rowid='rowid', tokenize='porter unicode61');
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER memories_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    INSERT INTO memories_fts (rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE focus_sessions (
    id         TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,      -- focus | short_break | long_break
    label      TEXT NOT NULL DEFAULT '',
    planned_s  INTEGER NOT NULL,
    actual_s   INTEGER NOT NULL,
    completed  INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL NOT NULL
);
CREATE INDEX focus_sessions_started ON focus_sessions (started_at);

CREATE TABLE mcp_servers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    command    TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '[]',
    env        TEXT NOT NULL DEFAULT '{}',
    enabled    INTEGER NOT NULL DEFAULT 1,
    -- Untrusted servers' outputs taint the run and their calls need approval.
    trusted    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
