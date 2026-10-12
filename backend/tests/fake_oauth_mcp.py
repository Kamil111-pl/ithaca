"""OAuth-protected MCP test server (auto-approves sign-in): python fake_oauth_mcp.py <port>."""
import secrets
import sys
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
                                      construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

port = int(sys.argv[1])
base = f"http://127.0.0.1:{port}"


class Provider:
    def __init__(self):
        self.clients, self.codes, self.tokens, self.refresh = {}, {}, {}, {}

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, info: OAuthClientInformationFull):
        self.clients[info.client_id] = info

    async def authorize(self, client, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, code):
        return self.codes.get(code)

    async def exchange_authorization_code(self, client, code: AuthorizationCode) -> OAuthToken:
        self.codes.pop(code.code, None)
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.tokens[access] = AccessToken(token=access, client_id=client.client_id, scopes=code.scopes,
                                          expires_at=int(time.time()) + 3600, resource=code.resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client.client_id, scopes=code.scopes)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh,
                          scope=" ".join(code.scopes) or None)

    async def load_refresh_token(self, client, token):
        return self.refresh.get(token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        access = secrets.token_urlsafe(24)
        self.tokens[access] = AccessToken(token=access, client_id=client.client_id, scopes=scopes,
                                          expires_at=int(time.time()) + 3600)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh_token.token)

    async def load_access_token(self, token):
        return self.tokens.get(token)

    async def revoke_token(self, token):
        self.tokens.pop(getattr(token, "token", None), None)


app = FastMCP(
    "oauth-fake", host="127.0.0.1", port=port, auth_server_provider=Provider(),
    auth=AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp",
                      client_registration_options=ClientRegistrationOptions(enabled=True)),
)


@app.tool()
def whoami() -> str:
    """Say who is signed in."""
    return "signed in via OAuth"


app.run(transport="streamable-http")
