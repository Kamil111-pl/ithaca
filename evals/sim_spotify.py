"""A fake Spotify Web API (httpx transport) for tests and evals.

Catalog spec (task setup "spotify"):
  {"tracks": [{"name", "artist", "album"?, "genres"?: [..]}],
   "playlists": [{"name", "owner"?, "description"?, "tracks": 50}],
   "devices": [{"name": "MacBook", "type": "Computer"}],   # [] = Spotify not open anywhere
   "premium": true, "playing": {"track": "<name>"}}
Records every play / skip / pause / queue so checks can see what happened.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())[:22] or "x"


class FakeSpotify:
    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        spec = spec or {}
        self.premium = spec.get("premium", True)
        self.tracks = [{"uri": f"spotify:track:{_slug(t['name'])}", "name": t["name"], "artist": t.get("artist", ""),
                        "album": t.get("album", t["name"]), "genres": t.get("genres", [])} for t in spec.get("tracks", [])]
        self.playlists = [{"uri": f"spotify:playlist:{_slug(p['name'])}", "name": p["name"], "owner": p.get("owner", "Spotify"),
                           "description": p.get("description", ""), "total": p.get("tracks", 40)}
                          for p in spec.get("playlists", [])]
        artists = {t["artist"] for t in self.tracks if t["artist"]}
        self.artists = [{"uri": f"spotify:artist:{_slug(a)}", "name": a,
                         "genres": sorted({g for t in self.tracks if t["artist"] == a for g in t["genres"]})} for a in artists]
        self.devices = [{"id": f"dev-{i}", "name": d.get("name", "MacBook"), "type": d.get("type", "Computer"),
                         "is_active": d.get("active", False)} for i, d in enumerate(spec.get("devices", [{"name": "MacBook"}]))]
        self.current: dict[str, Any] | None = None
        self.context: str | None = None
        self.is_playing = False
        if spec.get("playing"):
            t = self._track_named(spec["playing"]["track"])
            self.current, self.is_playing = t, True
        self.played: list[str] = []      # uris passed to play (tracks or contexts)
        self.actions: list[str] = []     # next, previous, pause, resume, queue:<uri>, volume:<n>
        self.searches: list[str] = []

    # -------------------------------------------------------------- helpers
    def _track_named(self, name: str) -> dict[str, Any] | None:
        return next((t for t in self.tracks if t["name"].lower() == name.lower()), None)

    def _match(self, q: str, *fields: str) -> bool:
        words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in {"genre", "artist", "track", "the", "a", "of", "in", "and", "music", "style", "like", "songs", "song"}]
        hay = " ".join(fields).lower()
        return bool(words) and sum(w in hay for w in words) >= max(1, (len(words) + 1) // 2)

    def _track_json(self, t: dict[str, Any]) -> dict[str, Any]:
        return {"uri": t["uri"], "name": t["name"], "artists": [{"name": t["artist"]}], "album": {"name": t["album"]}}

    def _state(self) -> dict[str, Any]:
        if not self.current:
            return {}
        dev = next((d for d in self.devices if d["is_active"]), self.devices[0] if self.devices else {})
        return {"is_playing": self.is_playing, "item": self._track_json(self.current),
                "context": {"uri": self.context} if self.context else None, "device": {"name": dev.get("name")}}

    # ------------------------------------------------------------ transport
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.replace("/v1", "", 1) if url.netloc == "api.spotify.com" else url.path
        if url.netloc == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "fake", "refresh_token": "fake-r", "expires_in": 3600})
        body = json.loads(req.content) if req.content else {}
        if path == "/me":
            return httpx.Response(200, json={"id": "user", "display_name": "Test User",
                                             "product": "premium" if self.premium else "free"})
        if path == "/search":
            query = q.get("q", "")
            self.searches.append(query)
            out: dict[str, Any] = {}
            kinds = q.get("type", "track").split(",")
            lim = int(q.get("limit", 5))
            if "track" in kinds:
                out["tracks"] = {"items": [self._track_json(t) for t in self.tracks
                                           if self._match(query, t["name"], t["artist"], t["album"], " ".join(t["genres"]))][:lim]}
            if "playlist" in kinds:
                out["playlists"] = {"items": [{"uri": p["uri"], "name": p["name"], "owner": {"display_name": p["owner"]},
                                               "tracks": {"total": p["total"]}} for p in self.playlists
                                              if self._match(query, p["name"], p["description"])][:lim]}
            if "artist" in kinds:
                out["artists"] = {"items": [{"uri": a["uri"], "name": a["name"], "genres": a["genres"]} for a in self.artists
                                            if self._match(query, a["name"], " ".join(a["genres"]))][:lim]}
            if "album" in kinds:
                out["albums"] = {"items": []}
            return httpx.Response(200, json=out)
        if path == "/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if path == "/me/player" and req.method == "GET":
            return httpx.Response(200, json=self._state()) if self.current else httpx.Response(204)
        # playback control
        if not self.premium:
            return httpx.Response(403, json={"error": {"status": 403, "message": "Player command failed: Premium required",
                                                       "reason": "PREMIUM_REQUIRED"}})
        if not self.devices:
            return httpx.Response(404, json={"error": {"status": 404, "message": "Player command failed: No active device found",
                                                       "reason": "NO_ACTIVE_DEVICE"}})
        dev_id = q.get("device_id")
        for d in self.devices:
            d["is_active"] = d["is_active"] or d["id"] == dev_id
        if path == "/me/player/play":
            if body.get("uris"):
                uri = body["uris"][0]
                t = next((t for t in self.tracks if t["uri"] == uri), None)
                if not t:
                    return httpx.Response(400, json={"error": {"status": 400, "message": "Invalid track uri"}})
                self.current, self.context = t, None
                self.played.append(uri)
            elif body.get("context_uri"):
                uri = body["context_uri"]
                if uri.startswith("spotify:playlist:") and not any(p["uri"] == uri for p in self.playlists) or \
                   uri.startswith("spotify:artist:") and not any(a["uri"] == uri for a in self.artists):
                    return httpx.Response(400, json={"error": {"status": 400, "message": "Invalid context uri"}})
                self.context = uri
                self.played.append(uri)
                self.current = self.tracks[0] if self.tracks else {"uri": "spotify:track:x", "name": "Track", "artist": "",
                                                                  "album": "", "genres": []}
                if uri.startswith("spotify:artist:"):
                    name = next(a["name"] for a in self.artists if a["uri"] == uri)
                    self.current = next(t for t in self.tracks if t["artist"] == name)
            else:
                self.actions.append("resume")
            self.is_playing = True
            return httpx.Response(204)
        if path == "/me/player/pause":
            self.is_playing = False
            self.actions.append("pause")
            return httpx.Response(204)
        if path in ("/me/player/next", "/me/player/previous"):
            self.actions.append(path.rsplit("/", 1)[1])
            if self.current and self.tracks:
                i = self.tracks.index(self.current) if self.current in self.tracks else 0
                self.current = self.tracks[(i + (1 if path.endswith("next") else -1)) % len(self.tracks)]
            return httpx.Response(204)
        if path == "/me/player/queue":
            self.actions.append(f"queue:{q.get('uri')}")
            return httpx.Response(204)
        if path == "/me/player/volume":
            self.actions.append(f"volume:{q.get('volume_percent')}")
            return httpx.Response(204)
        if path == "/me/player/shuffle":
            self.actions.append(f"shuffle:{q.get('state')}")
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"status": 404, "message": f"unknown endpoint {path}"}})


def connect(fake: FakeSpotify | None) -> None:
    """Point ithaca.spotify at the fake and mark the account connected (or not, when fake is None)."""
    from ithaca import db, settings, spotify

    if fake is None:
        spotify.transport = None
        return
    settings.set("spotify.client_id", "test-client")
    spotify.transport = fake.transport()
    db.execute("INSERT OR REPLACE INTO integrations (name, data, updated_at) VALUES ('spotify', ?, ?)",
               (db.dumps({"access_token": "fake", "refresh_token": "fake-r", "expires_at": 9e12,
                          "account": {"name": "Test User", "product": "premium" if fake.premium else "free"}}), db.now()))
