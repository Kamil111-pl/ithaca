-- Connected services (e.g. Spotify): OAuth tokens and per-service state.
-- Never exposed through /api/settings.
CREATE TABLE integrations (
    name       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
