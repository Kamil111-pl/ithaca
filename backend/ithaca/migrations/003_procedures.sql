-- Learned procedures: the tool steps of runs that worked, replayed as worked
-- examples for similar requests (see ithaca/procedures.py).
CREATE TABLE procedures (
    id            TEXT PRIMARY KEY,
    skeleton      TEXT NOT NULL,            -- JSON list of {"tool", "args"} with free text removed
    skeleton_hash TEXT NOT NULL UNIQUE,
    requests      TEXT NOT NULL,            -- recent user requests it solved, newline-separated
    successes     INTEGER NOT NULL DEFAULT 1,
    failures      INTEGER NOT NULL DEFAULT 0,
    embedding     BLOB,
    embed_model   TEXT,
    last_conversation_id TEXT,
    last_run_id   TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE VIRTUAL TABLE procedures_fts USING fts5 (requests, content='procedures', content_rowid='rowid', tokenize='porter unicode61');
CREATE TRIGGER procedures_ai AFTER INSERT ON procedures BEGIN
    INSERT INTO procedures_fts (rowid, requests) VALUES (new.rowid, new.requests);
END;
CREATE TRIGGER procedures_ad AFTER DELETE ON procedures BEGIN
    INSERT INTO procedures_fts (procedures_fts, rowid, requests) VALUES ('delete', old.rowid, old.requests);
END;
CREATE TRIGGER procedures_au AFTER UPDATE OF requests ON procedures BEGIN
    INSERT INTO procedures_fts (procedures_fts, rowid, requests) VALUES ('delete', old.rowid, old.requests);
    INSERT INTO procedures_fts (rowid, requests) VALUES (new.rowid, new.requests);
END;
