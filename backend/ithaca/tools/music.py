"""Music tools: search Spotify and control playback.

Two ways in, picked automatically (ithaca/spotify.py):
- a connected Spotify account (official Web API): best search, works on any device;
- no setup: the Spotify app on this Mac — a web search finds real Spotify links
  and Spotify's AppleScript plays them.
"""

from __future__ import annotations

from typing import Any

from .. import spotify
from ..spotify import SpotifyError
from .registry import ToolContext, ToolResult, obj, tool

KINDS = ["track", "artist", "album", "playlist"]
_KW = "spotify music song songs play listen artist album playlist track tune house techno jazz genre"
NO_SPOTIFY = ("Spotify isn't available: the Spotify app isn't installed on this Mac and no Spotify account is connected. "
              "Tell the user to install Spotify, or connect their account in Settings → Connected apps.")


def _line(n: int, s: dict[str, Any]) -> str:
    extra = f" — {s['by']}" if s.get("by") else ""
    if s["kind"] == "playlist" and s.get("tracks"):
        extra += f" ({s['tracks']} tracks)"
    if s["kind"] == "artist" and s.get("genres"):
        extra += f" [{s['genres']}]"
    return f"{n}. {s['kind']}: {s['name']}{extra}  uri={s['uri']}"


async def _now() -> dict[str, Any] | None:
    if spotify.mode() == "api":
        return await spotify.now_playing()
    return await spotify.desktop.now_playing() if spotify.desktop else None


@tool("spotify_search",
      "Search Spotify for tracks, artists, albums and playlists. For a mood or style (e.g. 'house music like Volen "
      "Sentir'), search playlists and tracks with the artist and genre words. Returns numbered results with uris to "
      "pass to spotify_play. Only describe music these results actually contain.",
      obj({"query": {"type": "string"},
           "kinds": {"type": "array", "items": {"type": "string", "enum": KINDS},
                     "description": "what to search (default: track and playlist)"},
           "limit": {"type": "integer", "description": "results per kind, max 10 (default 5)"}}, ["query"]),
      effects={"network"}, group="music", untrusted_output=True, keywords=_KW)
async def spotify_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    kinds = [k for k in (args.get("kinds") or ["track", "playlist"]) if k in KINDS] or ["track", "playlist"]
    limit = int(args.get("limit") or 5)
    mode = spotify.mode()
    try:
        if mode == "api":
            found = await spotify.search(args["query"], kinds, limit)
        elif mode == "desktop":
            found = await spotify.web_search(args["query"], kinds, limit)
            if not any(found.values()) and kinds != KINDS:  # widen before giving up
                found = await spotify.web_search(args["query"], KINDS, limit)
                kinds = KINDS
        else:
            return ToolResult(NO_SPOTIFY, is_error=True)
    except SpotifyError as exc:
        return ToolResult(str(exc), is_error=True)
    lines, n = [], 0
    for kind in kinds:
        for s in found.get(kind, []):
            n += 1
            lines.append(_line(n, s))
    if not lines:
        return ToolResult(f"Nothing on Spotify matched “{args['query']}”. Try fewer or different words.", is_error=True)
    how = "" if mode == "api" else " (found with a web search)"
    return ToolResult(f"Spotify results for “{args['query']}”{how}:\n" + "\n".join(lines), untrusted_source="spotify",
                      data={"results": [s for k in kinds for s in found.get(k, [])]})


@tool("spotify_play",
      "Play something on Spotify: pass a uri from spotify_search (track, album, playlist or artist), or a query to play "
      "the best match. Opens Spotify if needed and checks what really started playing.",
      obj({"uri": {"type": "string", "description": "spotify:track:…, spotify:playlist:…, spotify:album:… or spotify:artist:…"},
           "query": {"type": "string", "description": "used when no uri is given"},
           "kind": {"type": "string", "enum": KINDS, "description": "for query: what to look for (default playlist)"}}),
      effects={"network"}, group="music", keywords=_KW)
async def spotify_play(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    mode = spotify.mode()
    if mode == "none":
        return ToolResult(NO_SPOTIFY, is_error=True)
    try:
        uri = (args.get("uri") or "").strip()
        if uri.startswith("https://"):
            hit = spotify.link_to_uri(uri)
            uri = hit[1] if hit else uri
        if not uri:
            if not args.get("query"):
                return ToolResult("Give a uri from spotify_search, or a query.", is_error=True)
            kind = args.get("kind") if args.get("kind") in KINDS else "playlist"
            search = spotify.search if mode == "api" else spotify.web_search
            found = await search(args["query"], [kind, "track"] if kind != "track" else ["track"], 1)
            first = (found.get(kind) or []) + (found.get("track") or [])
            if not first:
                return ToolResult(f"Nothing on Spotify matched “{args['query']}”.", is_error=True)
            uri = first[0]["uri"]
        if not uri.startswith("spotify:"):
            return ToolResult("That isn't a Spotify uri (they look like spotify:playlist:…). Use one from spotify_search.",
                              is_error=True)
        if mode == "api":
            device = await spotify.ensure_device()
            if not device:
                return ToolResult("Spotify isn't running on any device. Ask the user to open the Spotify app, then try "
                                  "again.", is_error=True)
            body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
            await spotify.api("PUT", "/me/player/play", {"device_id": device}, body)
        else:
            await spotify.desktop.play(uri)
        now = await _now()
    except SpotifyError as exc:
        msg = str(exc)
        if msg == "NO_ACTIVE_DEVICE":
            msg = "Spotify has no active device. Ask the user to open Spotify and press play once, then try again."
        return ToolResult(msg, is_error=True)
    if not now or not now.get("is_playing"):
        return ToolResult("Spotify accepted the request but nothing is playing yet. Check with spotify_now_playing.",
                          is_error=True)
    started = now["uri"] == uri or now.get("context") == uri or not uri.startswith("spotify:track:")
    note = "" if started else f" (asked for {uri}, which may still be starting)"
    return ToolResult(f"Now playing: {now['name']} — {now['by']} on {now.get('device') or 'Spotify'}{note}.",
                      data={"now_playing": now, "uri": uri})


@tool("spotify_control",
      "Control Spotify: skip to the next song (next), go back (previous), pause, resume, shuffle_on/shuffle_off, or "
      "volume (0–100). Returns what's playing afterwards, so a skip already tells you the new song.",
      obj({"action": {"type": "string", "enum": ["pause", "resume", "next", "previous", "shuffle_on", "shuffle_off", "volume"]},
           "volume": {"type": "integer"}}, ["action"]),
      effects={"network"}, group="music", keywords=_KW + " pause skip next previous volume shuffle change song stop resume")
async def spotify_control(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    a = args["action"]
    mode = spotify.mode()
    if mode == "none":
        return ToolResult(NO_SPOTIFY, is_error=True)
    calls = {"pause": ("PUT", "/me/player/pause", None), "resume": ("PUT", "/me/player/play", None),
             "next": ("POST", "/me/player/next", None), "previous": ("POST", "/me/player/previous", None),
             "shuffle_on": ("PUT", "/me/player/shuffle", {"state": "true"}),
             "shuffle_off": ("PUT", "/me/player/shuffle", {"state": "false"})}
    try:
        vol = max(0, min(100, int(args.get("volume") if args.get("volume") is not None else 50)))
        if mode == "desktop":
            await spotify.desktop.control(a, vol)
        elif a == "volume":
            await spotify.api("PUT", "/me/player/volume", {"volume_percent": vol})
        else:
            method, path, params = calls[a]
            await spotify.api(method, path, params)
        if a == "volume":
            return ToolResult(f"Volume set to {vol}%.")
        now = await _now()
    except SpotifyError as exc:
        msg = "Nothing is playing on Spotify right now." if str(exc) == "NO_ACTIVE_DEVICE" else str(exc)
        return ToolResult(msg, is_error=True)
    tail = f" Now: {now['name']} — {now['by']}{'' if now['is_playing'] else ' (paused)'}." if now else ""
    return ToolResult(f"Done: {a.replace('_', ' ')}.{tail}", data={"now_playing": now})


@tool("spotify_now_playing",
      "What's playing on Spotify right now (only reads it — to skip, pause or change songs use spotify_control).",
      effects={"network"}, group="music", keywords=_KW + " now playing current song what")
async def spotify_now_playing(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if spotify.mode() == "none":
        return ToolResult(NO_SPOTIFY, is_error=True)
    try:
        now = await _now()
    except SpotifyError as exc:
        msg = "Nothing is playing on Spotify right now." if str(exc) == "NO_ACTIVE_DEVICE" else str(exc)
        return ToolResult(msg, is_error=str(exc) != "NO_ACTIVE_DEVICE")
    if not now:
        return ToolResult("Nothing is playing on Spotify right now.")
    return ToolResult(f"{'Playing' if now['is_playing'] else 'Paused'}: {now['name']} — {now['by']}"
                      + (f" on {now['device']}" if now.get("device") else "") + ".", data={"now_playing": now})


@tool("spotify_queue", "Add a track (uri from spotify_search) to the Spotify queue (needs a connected account).",
      obj({"uri": {"type": "string"}}, ["uri"]), effects={"network"}, group="music", keywords=_KW + " queue add next up")
async def spotify_queue(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not str(args["uri"]).startswith("spotify:track:"):
        return ToolResult("Only tracks can be queued (spotify:track:…).", is_error=True)
    if spotify.mode() != "api":
        return ToolResult("Queueing needs a connected Spotify account (Settings → Connected apps). Playing a track "
                          "right away works without it.", is_error=True)
    try:
        await spotify.api("POST", "/me/player/queue", {"uri": args["uri"]})
    except SpotifyError as exc:
        return ToolResult("Spotify has no active device to queue on." if str(exc) == "NO_ACTIVE_DEVICE" else str(exc),
                          is_error=True)
    return ToolResult("Added to the queue.")
