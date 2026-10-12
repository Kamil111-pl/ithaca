-- Remote MCP servers: Streamable HTTP / SSE transports, static headers, OAuth.
ALTER TABLE mcp_servers ADD COLUMN transport TEXT NOT NULL DEFAULT 'stdio';   -- stdio | http | sse | auto
ALTER TABLE mcp_servers ADD COLUMN url TEXT NOT NULL DEFAULT '';
ALTER TABLE mcp_servers ADD COLUMN headers TEXT NOT NULL DEFAULT '{}';         -- e.g. {"Authorization": "Bearer …"}
ALTER TABLE mcp_servers ADD COLUMN auth TEXT NOT NULL DEFAULT 'none';          -- none | headers | oauth
ALTER TABLE mcp_servers ADD COLUMN oauth TEXT NOT NULL DEFAULT '{}';           -- {"tokens": …, "client_info": …}
