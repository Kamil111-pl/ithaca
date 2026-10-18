"""Music tools: search Spotify and control playback on the user's account (ithaca/spotify.py)."""

from __future__ import annotations

from typing import Any

from .. import spotify
from ..spotify import SpotifyError
from .registry import ToolContext, ToolResult, obj, tool

KINDS = ["track", "artist", "album", "playlist"]
_KW = "spotify music song songs play listen artist album playlist track tune house techno jazz genre"


def _line(n: int, s: dict[str, Any]) -> str:
    extra = f" — {s['by']}" if s.get("by") else ""
    if s["kind"] == "playlist" and s.get("tracks"):
        extra += f" ({s['tracks']} tracks)"
    if s["kind"] == "artist" and s.get("genres"):
        extra += f" [{s['genres']}]"
    return f"{n}. {s['kind']}: {s['name']}{extra}  uri={s['uri']}"


@tool("spotify_search",
      "Search Spotify for tracks, artists, albums and playlists. For a mood or style (e.g. 'house music like Volen "
      "Sentir'), search playlists and tracks with the artist and genre words. Returns numbered results with uris to "
      "pass to spotify_play.",
      obj({"query": {"type": "string"},
           "kinds": {"type": "array", "items": {"type": "string", "enum": KINDS},
                     "description": "what to search (default: track and playlist)"},
           "limit": {"type": "integer", "description": "results per kind, max 10 (default 5)"}}, ["query"]),
      effects={"network"}, group="music", untrusted_output=True, keywords=_KW)
async def spotify_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    kinds = [k for k in (args.get("kinds") or ["track", "playlist"]) if k in KINDS] or ["track", "playlist"]
    try:
        found = await spotify.search(args["query"], kinds, int(args.get("limit") or 5))
    except SpotifyError as exc:
        return ToolResult(str(exc), is_error=True)
    lines, n = [], 0
    for kind in kinds:
        for s in found.get(kind, []):
            n += 1
            lines.append(_line(n, s))
    if not lines:
        return ToolResult(f"Nothing on Spotify matched “{args['query']}”. Try fewer or different words.", is_error=True)
    return ToolResult(f"Spotify results for “{args['query']}”:\n" + "\n".join(lines), untrusted_source="spotify",
                      data={"results": [s for k in kinds for s in found.get(k, [])]})


@tool("spotify_play",
      "Play something on the user's Spotify: pass a uri from spotify_search (track, album, playlist or artist), or a "
      "query to play the best match. Opens Spotify on this Mac if no device is active, and checks that it really started.",
      obj({"uri": {"type": "string", "description": "spotify:track:…, spotify:playlist:…, spotify:album:… or spotify:artist:…"},
           "query": {"type": "string", "description": "used when no uri is given"},
           "kind": {"type": "string", "enum": KINDS, "description": "for query: what to look for (default track)"}}),
      effects={"network"}, group="music", keywords=_KW)
async def spotify_play(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        uri = (args.get("uri") or "").strip()
        if not uri:
            if not args.get("query"):
                return ToolResult("Give a uri from spotify_search, or a query.", is_error=True)
            kind = args.get("kind") if args.get("kind") in KINDS else "track"
            found = (await spotify.search(args["query"], [kind], 1)).get(kind) or []
            if not found:
                return ToolResult(f"Nothing on Spotify matched “{args['query']}”.", is_error=True)
            uri = found[0]["uri"]
        if not uri.startswith("spotify:"):
            return ToolResult("That isn't a Spotify uri (they look like spotify:track:…). Use one from spotify_search.",
                              is_error=True)
        device = await spotify.ensure_device()
        if not device:
            return ToolResult("Spotify isn't running on any device. Ask the user to open the Spotify app, then try again.",
                              is_error=True)
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        await spotify.api("PUT", "/me/player/play", {"device_id": device}, body)
        # verify: what is actually playing now?
        now = await spotify.now_playing()
    except SpotifyError as exc:
        msg = str(exc)
        if msg == "NO_ACTIVE_DEVICE":
            msg = "Spotify has no active device. Ask the user to open Spotify and press play once, then try again."
        return ToolResult(msg, is_error=True)
    if not now:
        return ToolResult("Spotify accepted the request but nothing is playing yet. Check with spotify_now_playing.",
                          is_error=True)
    started = now["uri"] == uri or now.get("context") == uri
    note = "" if started else f" (asked for {uri}, which may still be starting)"
    return ToolResult(f"Now playing: {now['name']} — {now['by']} on {now.get('device') or 'Spotify'}{note}.",
                      data={"now_playing": now})


@tool("spotify_control",
      "Control Spotify: skip to the next song (next), go back (previous), pause, resume, shuffle_on/shuffle_off, or "
      "volume (0–100). Returns what's playing afterwards, so a skip already tells you the new song.",
      obj({"action": {"type": "string", "enum": ["pause", "resume", "next", "previous", "shuffle_on", "shuffle_off", "volume"]},
           "volume": {"type": "integer"}}, ["action"]),
      effects={"network"}, group="music", keywords=_KW + " pause skip next previous volume shuffle change song stop resume")
async def spotify_control(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    a = args["action"]
    calls = {"pause": ("PUT", "/me/player/pause", None), "resume": ("PUT", "/me/player/play", None),
             "next": ("POST", "/me/player/next", None), "previous": ("POST", "/me/player/previous", None),
             "shuffle_on": ("PUT", "/me/player/shuffle", {"state": "true"}),
             "shuffle_off": ("PUT", "/me/player/shuffle", {"state": "false"})}
    try:
        if a == "volume":
            v = max(0, min(100, int(args.get("volume") if args.get("volume") is not None else 50)))
            await spotify.api("PUT", "/me/player/volume", {"volume_percent": v})
            return ToolResult(f"Volume set to {v}%.")
        method, path, params = calls[a]
        await spotify.api(method, path, params)
        now = await spotify.now_playing()
    except SpotifyError as exc:
        msg = "Nothing is playing on Spotify right now." if str(exc) == "NO_ACTIVE_DEVICE" else str(exc)
        return ToolResult(msg, is_error=True)
    tail = f" Now: {now['name']} — {now['by']}{'' if now['is_playing'] else ' (paused)'}." if now else ""
    return ToolResult(f"Done: {a.replace('_', ' ')}.{tail}", data={"now_playing": now})


@tool("spotify_now_playing", "What's playing on the user's Spotify right now (only reads it — to skip, pause or change "
      "songs use spotify_control).",
      effects={"network"}, group="music", keywords=_KW + " now playing current song what")
async def spotify_now_playing(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        now = await spotify.now_playing()
    except SpotifyError as exc:
        msg = "Nothing is playing on Spotify right now." if str(exc) == "NO_ACTIVE_DEVICE" else str(exc)
        return ToolResult(msg, is_error=str(exc) != "NO_ACTIVE_DEVICE")
    if not now:
        return ToolResult("Nothing is playing on Spotify right now.")
    return ToolResult(f"{'Playing' if now['is_playing'] else 'Paused'}: {now['name']} — {now['by']}"
                      + (f" on {now['device']}" if now.get("device") else "") + ".", data={"now_playing": now})


@tool("spotify_queue", "Add a track (uri from spotify_search) to the user's Spotify queue.",
      obj({"uri": {"type": "string"}}, ["uri"]), effects={"network"}, group="music", keywords=_KW + " queue add next up")
async def spotify_queue(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not str(args["uri"]).startswith("spotify:track:"):
        return ToolResult("Only tracks can be queued (spotify:track:…).", is_error=True)
    try:
        await spotify.api("POST", "/me/player/queue", {"uri": args["uri"]})
    except SpotifyError as exc:
        return ToolResult("Spotify has no active device to queue on." if str(exc) == "NO_ACTIVE_DEVICE" else str(exc),
                          is_error=True)
    return ToolResult("Added to the queue.")
