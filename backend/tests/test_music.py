import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))

from ithaca import settings, spotify  # noqa: E402
from ithaca.tools import music  # noqa: E402,F401
from ithaca.tools.registry import ToolContext, registry  # noqa: E402
from sim_spotify import FakeDesktop, FakeSpotify, connect  # noqa: E402

CATALOG = {
    "tracks": [{"name": "Bloom", "artist": "Volen Sentir", "genres": ["melodic house"]},
               {"name": "Nightfall", "artist": "Volen Sentir", "genres": ["melodic house"]},
               {"name": "Sunburst", "artist": "Other Artist", "genres": ["pop"]}],
    "playlists": [{"name": "Volen Sentir Radio", "description": "melodic house like Volen Sentir"},
                  {"name": "Melodic House Essentials", "description": "deep melodic house"}],
    "devices": [{"name": "MacBook Pro", "type": "Computer"}],
}


async def tool(_name, **args):
    return await registry.get(_name).handler(args, ToolContext(run_id="r", conversation_id="c"))


@pytest.fixture
def fake():
    f = FakeSpotify(CATALOG)
    connect(f)
    yield f
    connect(None)


async def test_search_then_play_a_playlist_and_verify(fake):
    r = await tool("spotify_search", query="melodic house Volen Sentir", kinds=["playlist", "track"])
    assert not r.is_error and "Volen Sentir Radio" in r.content and "spotify:playlist:" in r.content
    uri = next(s["uri"] for s in r.data["results"] if s["name"] == "Volen Sentir Radio")
    r = await tool("spotify_play", uri=uri)
    assert not r.is_error and "Now playing" in r.content and "MacBook Pro" in r.content
    assert fake.played == [uri]


async def test_play_by_query_and_control(fake):
    r = await tool("spotify_play", query="Bloom Volen Sentir")
    assert not r.is_error and "Bloom" in r.content
    r = await tool("spotify_control", action="next")
    assert not r.is_error and "Nightfall" in r.content and fake.actions == ["next"]
    r = await tool("spotify_now_playing")
    assert "Nightfall" in r.content


async def test_clear_errors_when_not_connected_no_device_or_no_premium():
    connect(None)
    settings.set("spotify.client_id", "")
    r = await tool("spotify_search", query="anything")
    assert r.is_error and "Settings → Connected apps" in r.content
    f = FakeSpotify({**CATALOG, "devices": []})
    connect(f)
    try:
        r = await tool("spotify_play", query="Bloom")
        assert r.is_error and "open the Spotify app" in r.content
        f2 = FakeSpotify({**CATALOG, "premium": False})
        connect(f2)
        r = await tool("spotify_play", query="Bloom")
        assert r.is_error and "Premium" in r.content
    finally:
        connect(None)


def test_tokens_are_not_exposed_through_settings(fake):
    assert "spotify.tokens" not in settings.all_public()
    assert all("fake" != v for v in settings.all_public().values())
    assert spotify.status()["connected"] is True


def test_sign_in_url_uses_pkce_and_loopback_redirect():
    settings.set("spotify.client_id", "abc123")
    url = spotify.start_auth()
    assert "code_challenge_method=S256" in url and "client_id=abc123" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A" in url and "client_secret" not in url


async def test_no_setup_mode_uses_web_search_and_the_spotify_app(monkeypatch):
    from ithaca import web

    desk = FakeDesktop(CATALOG)
    connect(None, desk)
    pages = desk.web_pages()

    async def fake_search(q, limit=8):
        return [{"title": v.splitlines()[0], "url": k, "snippet": ""} for k, v in pages.items()
                if any(w in (k + v).lower() for w in q.lower().split() if w not in ("spotify", "playlist", "track"))][:limit]

    monkeypatch.setattr(web, "search", fake_search)
    try:
        assert spotify.mode() == "desktop"
        r = await tool("spotify_search", query="volen sentir", kinds=["playlist", "track"])
        assert not r.is_error and "Volen Sentir Radio" in r.content and "web search" in r.content
        uri = next(s["uri"] for s in r.data["results"] if s["name"] == "Volen Sentir Radio")
        r = await tool("spotify_play", uri=uri)
        assert not r.is_error and "Now playing" in r.content and desk.played == [uri]
        r = await tool("spotify_control", action="next")
        assert not r.is_error and desk.actions == ["next"]
        r = await tool("spotify_queue", uri="spotify:track:" + "a" * 22)
        assert r.is_error and "connected" in r.content
    finally:
        connect(None, None)


def test_spotify_links_become_uris():
    assert spotify.link_to_uri("https://open.spotify.com/intl-es/artist/7scXA3hBD8JyGGajVR9q9l") == \
        ("artist", "spotify:artist:7scXA3hBD8JyGGajVR9q9l")
    assert spotify.link_to_uri("https://example.com/playlist/x") is None
    assert spotify._clean_title("Deep House Relax - playlist by Spotify | Spotify") == ("Deep House Relax", "Spotify")
