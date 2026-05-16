"""
Muse — server.py  v2.0
━━━━━━━━━━━━━━━━━━━━━━

SETUP (run these once in your project folder):
  pip install ytmusicapi flask flask-cors yt-dlp

OPTIONAL — log in to access your YTM library & playlists:
  py -c "from ytmusicapi import YTMusic; YTMusic.setup(filepath='oauth.json')"

RUN:
  py server.py

Then open index.html in your browser.
"""

from flask import Flask, jsonify, request, Response, stream_with_context, session
from flask_cors import CORS
from ytmusicapi import YTMusic
import yt_dlp, traceback, os, json, sqlite3, hashlib, secrets, functools

DB_PATH = "muse.db"

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT    UNIQUE NOT NULL,
                email     TEXT    UNIQUE NOT NULL,
                password  TEXT    NOT NULL,
                created   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token     TEXT    PRIMARY KEY,
                user_id   INTEGER NOT NULL,
                created   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS playlists (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                name      TEXT    NOT NULL,
                ytm_id    TEXT,
                created   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_id INTEGER NOT NULL,
                video_id    TEXT    NOT NULL,
                title       TEXT,
                artist      TEXT,
                thumbnail   TEXT,
                duration    TEXT,
                added       TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (playlist_id, video_id)
            );
            CREATE TABLE IF NOT EXISTS listen_history (
                user_id   INTEGER NOT NULL,
                video_id  TEXT    NOT NULL,
                title     TEXT,
                artist    TEXT,
                thumbnail TEXT,
                played_at TEXT    DEFAULT (datetime('now'))
            );
        """)

init_db()

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_current_user(req):
    token = req.headers.get("X-Auth-Token") or req.args.get("token")
    if not token:
        return None
    with get_db() as db:
        row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token,)).fetchone()
    return dict(row) if row else None

def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user(request)
        if not user:
            return jsonify({"error": "Not logged in"}), 401
        return f(user, *args, **kwargs)
    return wrapper

VERSION = "3.0"

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ─── Auth ──────────────────────────────────────────────────────────────────────
OAUTH_FILE = "oauth.json"

def make_client():
    if os.path.exists(OAUTH_FILE):
        try:
            c = YTMusic(OAUTH_FILE)
            print(f"[muse] oauth.json loaded — library access enabled")
            return c, True
        except Exception as e:
            print(f"[warn] oauth.json failed: {e}")
    print(f"[muse] No oauth.json — search & charts work, library needs login")
    return YTMusic(), False

yt, authenticated = make_client()

# ─── Helpers ───────────────────────────────────────────────────────────────────
def best_thumb(thumbnails):
    """
    Handles both thumbnail formats ytmusicapi returns:
      search:         item["thumbnails"] = [{url,width,height},...]  (list)
      watch_playlist: item["thumbnail"]  = {"thumbnails":[...]}      (dict)
    """
    if not thumbnails:
        return None
    if isinstance(thumbnails, dict):
        thumbnails = thumbnails.get("thumbnails", [])
    if not thumbnails or not isinstance(thumbnails, list):
        return None
    try:
        best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
        url  = best.get("url", "")
        return ("https:" + url) if url.startswith("//") else url
    except Exception:
        try:
            url = thumbnails[-1].get("url", "")
            return ("https:" + url) if url.startswith("//") else url
        except Exception:
            return None

def fmt_artists(artists):
    """
    ytmusicapi always returns artists as list of {"name": str, "id": str|None}
    Never a string. Never called artistNames.
    """
    if not artists:
        return "Unknown Artist"
    if isinstance(artists, list):
        parts = [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
        return ", ".join(parts) if parts else "Unknown Artist"
    if isinstance(artists, str):
        return artists
    return "Unknown Artist"

def norm_track(item, rank=None):
    """
    Build a clean track dict from a ytmusicapi result.

    CONFIRMED field names in ytmusicapi 1.11.5:
      item["title"]      — str
      item["artists"]    — list of {name, id}
      item["album"]      — {name, id} dict  OR  None
      item["videoId"]    — str
      item["duration"]   — str "3:45"  OR  None
      item["thumbnails"] — list of {url, width, height}
    """
    if not item:
        return None

    album = item.get("album")
    album_name = None
    if isinstance(album, dict):
        album_name = album.get("name")
    elif isinstance(album, str):
        album_name = album

    t = {
        "videoId":   item.get("videoId"),
        "title":     item.get("title") or "Unknown",
        "artist":    fmt_artists(item.get("artists")),
        "album":     album_name,
        "duration":  item.get("duration"),
        # search → item["thumbnails"] (list)
        # watch_playlist → item["thumbnail"] (dict wrapping list)
        "thumbnail": best_thumb(item.get("thumbnails") or item.get("thumbnail")),
        "explicit":  item.get("isExplicit", False),
    }
    if rank is not None:
        t["rank"] = rank
    return t

def err(msg, code=500):
    print(f"[error {code}] {msg}")
    return jsonify({"error": str(msg)}), code

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "authenticated": authenticated, "version": VERSION})


@app.route("/search")
def search():
    """
    GET /search?q=taylor+swift&filter=songs&limit=25
    GET /search?q=taylor+swift&filter=artists&limit=10

    filter: songs | videos | albums | artists
    Songs/videos: returns tracks with videoId
    Artists: returns artist objects with browseId for the artist page
    """
    q       = request.args.get("q", "").strip()
    filter_ = request.args.get("filter", "songs")
    limit   = min(int(request.args.get("limit", 25)), 50)

    if not q:
        return err("Missing ?q=", 400)

    VALID = {"songs", "videos", "albums", "artists", "playlists",
             "community_playlists", "featured_playlists", "uploads"}
    if filter_ not in VALID:
        filter_ = "songs"

    try:
        raw = yt.search(q, filter=filter_, limit=limit)

        if filter_ == "artists":
            artists = []
            for item in (raw or []):
                browse_id = item.get("browseId")
                if not browse_id:
                    continue
                artists.append({
                    "browseId":  browse_id,
                    "name":      item.get("artist") or item.get("title") or "Unknown",
                    "thumbnail": best_thumb(item.get("thumbnails")),
                    "subscribers": item.get("subscribers"),
                })
            print(f"[search] '{q}' filter=artists → {len(artists)} artists")
            return jsonify({"artists": artists, "total": len(artists)})

        tracks = []
        for item in (raw or []):
            if not item.get("videoId"):
                continue
            t = norm_track(item)
            if t:
                tracks.append(t)
        print(f"[search] '{q}' filter={filter_} → {len(tracks)} tracks")
        return jsonify({"tracks": tracks, "total": len(tracks)})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/artist/<browse_id>")
def artist_page(browse_id):
    """
    GET /artist/<browseId>

    Returns full artist profile:
      name, thumbnail, description, top songs, albums, singles
    """
    try:
        data = yt.get_artist(browse_id)

        # Top songs
        songs_data = data.get("songs", {})
        top_songs  = []
        for item in (songs_data.get("results") or [])[:10]:
            if not item.get("videoId"):
                continue
            t = norm_track(item)
            if t:
                top_songs.append(t)

        # Albums
        albums_data = data.get("albums", {})
        albums = []
        for item in (albums_data.get("results") or [])[:12]:
            albums.append({
                "browseId":  item.get("browseId"),
                "playlistId": item.get("playlistId"),
                "title":     item.get("title") or "Unknown Album",
                "year":      item.get("year"),
                "thumbnail": best_thumb(item.get("thumbnails")),
                "type":      item.get("type", "Album"),
            })

        # Singles
        singles_data = data.get("singles", {})
        singles = []
        for item in (singles_data.get("results") or [])[:8]:
            singles.append({
                "browseId":  item.get("browseId"),
                "playlistId": item.get("playlistId"),
                "title":     item.get("title") or "Unknown",
                "year":      item.get("year"),
                "thumbnail": best_thumb(item.get("thumbnails")),
            })

        # Related artists
        related_data = data.get("related", {})
        related = []
        for item in (related_data.get("results") or [])[:6]:
            related.append({
                "browseId":  item.get("browseId"),
                "name":      item.get("title") or "Unknown",
                "thumbnail": best_thumb(item.get("thumbnails")),
                "subscribers": item.get("subscribers"),
            })

        return jsonify({
            "name":        data.get("name") or data.get("title") or "Unknown Artist",
            "thumbnail":   best_thumb(data.get("thumbnails")),
            "description": data.get("description"),
            "subscribers": data.get("subscribers"),
            "views":       data.get("views"),
            "topSongs":    top_songs,
            "albums":      albums,
            "singles":     singles,
            "related":     related,
        })
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/charts")
def charts():
    """
    GET /charts

    Strategy:
    1. Fetch Billboard Hot 100 RSS feed (updated weekly, always current)
    2. For each chart entry, search YTM to get a playable videoId + artwork
    3. Return ranked tracks

    Falls back to iTunes top songs API if Billboard is unreachable.
    """
    import urllib.request, xml.etree.ElementTree as ET, json as json_mod, time

    def fetch_billboard_titles():
        """Scrape Billboard Hot 100 from their JSON-LD chart page."""
        url = "https://www.billboard.com/charts/hot-100/"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                html = r.read().decode("utf-8", errors="ignore")
            # Extract song titles from the chart HTML
            import re
            # Billboard embeds chart data as JSON in a script tag
            match = re.findall(r'"title"\s*:\s*"([^"]{2,60})".*?"artist"\s*:\s*"([^"]{2,60})"', html)
            seen_titles = set()
            results = []
            for title, artist in match[:30]:
                key = title.lower()
                if key not in seen_titles and not title.startswith("http"):
                    seen_titles.add(key)
                    results.append((title, artist))
                if len(results) >= 25:
                    break
            return results
        except Exception as e:
            print(f"[charts] Billboard failed: {e}")
            return []

    def fetch_itunes_titles():
        """iTunes top 100 songs — always current, free API."""
        url = "https://itunes.apple.com/us/rss/topsongs/limit=40/json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json_mod.loads(r.read())
            entries = data.get("feed", {}).get("entry", [])
            results = []
            for e in entries:
                title  = e.get("im:name", {}).get("label", "")
                artist = e.get("im:artist", {}).get("label", "")
                if title and artist:
                    results.append((title, artist))
            return results
        except Exception as e:
            print(f"[charts] iTunes fallback failed: {e}")
            return []

    try:
        # Try Billboard first, fall back to iTunes
        chart_entries = fetch_billboard_titles()
        source = "Billboard Hot 100"
        if not chart_entries:
            chart_entries = fetch_itunes_titles()
            source = "iTunes Top Songs"

        if not chart_entries:
            return jsonify({"tracks": [], "source": "none"})

        print(f"[charts] Got {len(chart_entries)} entries from {source}")

        # Search YTM for each chart entry to get videoId + artwork
        tracks = []
        seen_vids = set()
        for rank, (title, artist) in enumerate(chart_entries[:25], start=1):
            try:
                query = f"{title} {artist}"
                results = yt.search(query, filter="songs", limit=3)
                for item in (results or []):
                    vid = item.get("videoId")
                    if vid and vid not in seen_vids:
                        seen_vids.add(vid)
                        t = norm_track(item, rank=rank)
                        # Override with Billboard's authoritative title/artist
                        t["title"]  = title
                        t["artist"] = artist
                        tracks.append(t)
                        break
            except Exception:
                pass
            time.sleep(0.05)  # be gentle with YTM

        print(f"[charts] Resolved {len(tracks)} tracks with videoIds")
        return jsonify({"tracks": tracks, "source": source})

    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/recommend", methods=["POST"])
def recommend():
    """
    POST /recommend
    Body: { "videoIds": ["id1","id2",...] }  (up to 5 seed tracks from listen history)

    Calls get_watch_playlist for each seed, merges and deduplicates results.
    This gives a recommendation list based on everything the user has listened to,
    not just the last song.

    get_watch_playlist thumbnails come back differently from search results —
    they're in item["thumbnail"]["thumbnails"] (singular key wrapping the list).
    norm_track handles both shapes.
    """
    import time
    try:
        body      = request.get_json(force=True) or {}
        video_ids = (body.get("videoIds") or [])[:5]   # max 5 seeds
        if not video_ids:
            return err("videoIds required", 400)

        seen   = set(video_ids)   # exclude seeds from results
        tracks = []

        for vid in video_ids:
            try:
                data = yt.get_watch_playlist(videoId=vid, limit=20)
                for item in data.get("tracks", []):
                    item_vid = item.get("videoId")
                    if not item_vid or item_vid in seen:
                        continue
                    seen.add(item_vid)
                    t = norm_track(item)
                    if t and t.get("thumbnail"):
                        tracks.append(t)
                    elif t:
                        tracks.append(t)
                time.sleep(0.05)
            except Exception as e:
                print(f"[recommend] seed {vid} failed: {e}")
                continue

        # Shuffle so it's not just the last song's radio
        import random
        random.shuffle(tracks)

        print(f"[recommend] {len(tracks)} tracks from {len(video_ids)} seeds")
        return jsonify({"tracks": tracks[:30]})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/suggestions")
def suggestions():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"suggestions": []})
    try:
        results = yt.get_search_suggestions(q) or []
        return jsonify({"suggestions": [s for s in results if isinstance(s, str)][:8]})
    except Exception:
        return jsonify({"suggestions": []})


@app.route("/stream/<video_id>")
def stream_audio(video_id):
    """
    GET /stream/<videoId>

    Uses yt-dlp to get a fresh, signed audio URL from YouTube and
    redirects the browser to it. This is the only reliable way to get
    playable audio — ytmusicapi's streamingData URLs expire in seconds.

    yt-dlp must be installed: pip install yt-dlp
    """
    try:
        ydl_opts = {
            "format":      "bestaudio[ext=m4a]/bestaudio/best",
            "quiet":       True,
            "no_warnings": True,
            "skip_download": True,
            # Don't cache — always get a fresh URL
            "noplaylist": True,
        }
        url = f"https://music.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # info may be a playlist wrapper
            if "entries" in info:
                info = info["entries"][0]
            audio_url = info.get("url")
            duration  = info.get("duration", 0)
            title     = info.get("title", "")
            if not audio_url:
                return err("No audio URL found", 404)

        print(f"[stream] {video_id} → {audio_url[:60]}…")
        # Return the direct URL so the browser can stream it natively
        return jsonify({
            "audioUrl": audio_url,
            "duration": duration,
            "title":    title,
        })
    except Exception as e:
        traceback.print_exc()
        return err(f"yt-dlp failed: {str(e)}")


@app.route("/watch/<video_id>")
def watch_playlist(video_id):
    """GET /watch/<videoId> — Up Next queue"""
    try:
        data   = yt.get_watch_playlist(videoId=video_id, limit=20)
        tracks = []
        for item in data.get("tracks", []):
            if not item.get("videoId"):
                continue
            t = norm_track(item)
            if t:
                tracks.append(t)
        return jsonify({"tracks": tracks})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


# ─── Authenticated routes ──────────────────────────────────────────────────────

@app.route("/library/songs")
def library_songs():
    if not authenticated:
        return err("Not authenticated", 401)
    try:
        songs  = yt.get_library_songs(limit=100) or []
        tracks = [t for t in [norm_track(s) for s in songs if s.get("videoId")] if t]
        return jsonify({"tracks": tracks})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/library/playlists")
def library_playlists():
    if not authenticated:
        return err("Not authenticated", 401)
    try:
        pls = yt.get_library_playlists(limit=50) or []
        return jsonify({"playlists": [
            {"id": p.get("playlistId"), "title": p.get("title"),
             "count": p.get("count", 0), "thumbnail": best_thumb(p.get("thumbnails"))}
            for p in pls if p.get("playlistId")
        ]})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/playlist/<playlist_id>")
def playlist_tracks(playlist_id):
    try:
        data   = yt.get_playlist(playlist_id, limit=100)
        tracks = [t for t in [norm_track(x) for x in data.get("tracks", []) if x.get("videoId")] if t]
        return jsonify({"title": data.get("title"), "thumbnail": best_thumb(data.get("thumbnails")), "tracks": tracks})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/playlist/create", methods=["POST"])
def create_playlist():
    if not authenticated:
        return err("Not authenticated", 401)
    body  = request.get_json(force=True) or {}
    title = body.get("title", "").strip()
    if not title:
        return err("title required", 400)
    try:
        pid = yt.create_playlist(title, body.get("description", ""))
        return jsonify({"playlistId": pid})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/playlist/<playlist_id>/add", methods=["POST"])
def add_to_playlist(playlist_id):
    if not authenticated:
        return err("Not authenticated", 401)
    body      = request.get_json(force=True) or {}
    video_ids = body.get("videoIds", [])
    if not video_ids:
        return err("videoIds required", 400)
    try:
        yt.add_playlist_items(playlist_id, video_ids)
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


@app.route("/like/<video_id>", methods=["POST"])
def like_song(video_id):
    if not authenticated:
        return err("Not authenticated", 401)
    try:
        yt.rate_song(video_id, "LIKE")
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return err(str(e))


# ─── Account routes ─────────────────────────────────────────────────────────────

@app.route("/auth/register", methods=["POST"])
def register():
    body     = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    email    = (body.get("email")    or "").strip().lower()
    password = (body.get("password") or "").strip()

    if not username or not email or not password:
        return err("username, email and password required", 400)
    if len(password) < 6:
        return err("Password must be at least 6 characters", 400)

    try:
        with get_db() as db:
            db.execute("INSERT INTO users (username,email,password) VALUES (?,?,?)",
                       (username, email, hash_pw(password)))
        return jsonify({"ok": True})
    except sqlite3.IntegrityError as e:
        return err("Username or email already taken", 409)
    except Exception as e:
        return err(str(e))


@app.route("/auth/login", methods=["POST"])
def login():
    body     = request.get_json(force=True) or {}
    email    = (body.get("email")    or "").strip().lower()
    password = (body.get("password") or "").strip()

    if not email or not password:
        return err("email and password required", 400)

    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=? AND password=?",
                          (email, hash_pw(password))).fetchone()
        if not user:
            return err("Invalid email or password", 401)
        token = secrets.token_hex(32)
        db.execute("INSERT INTO sessions (token,user_id) VALUES (?,?)", (token, user["id"]))

    return jsonify({"ok": True, "token": token, "username": user["username"], "userId": user["id"]})


@app.route("/auth/logout", methods=["POST"])
@require_auth
def logout(user):
    token = request.headers.get("X-Auth-Token")
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
    return jsonify({"ok": True})


@app.route("/auth/me")
@require_auth
def me(user):
    return jsonify({"userId": user["id"], "username": user["username"], "email": user["email"]})


# ─── User playlists (DB-backed) ───────────────────────────────────────────────

@app.route("/user/playlists")
@require_auth
def user_playlists(user):
    with get_db() as db:
        pls = db.execute("SELECT * FROM playlists WHERE user_id=? ORDER BY created DESC", (user["id"],)).fetchall()
        result = []
        for pl in pls:
            tracks = db.execute("SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY added", (pl["id"],)).fetchall()
            result.append({
                "id":        pl["id"],
                "name":      pl["name"],
                "ytmId":     pl["ytm_id"],
                "tracks":    [dict(t) for t in tracks],
                "thumbnail": dict(tracks[0])["thumbnail"] if tracks else None,
            })
    return jsonify({"playlists": result})


@app.route("/user/playlists/create", methods=["POST"])
@require_auth
def user_create_playlist(user):
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return err("name required", 400)
    with get_db() as db:
        cur = db.execute("INSERT INTO playlists (user_id,name) VALUES (?,?)", (user["id"], name))
        pl_id = cur.lastrowid
    return jsonify({"ok": True, "id": pl_id, "name": name})


@app.route("/user/playlists/<int:pl_id>/add", methods=["POST"])
@require_auth
def user_add_to_playlist(user, pl_id):
    # Verify ownership
    with get_db() as db:
        pl = db.execute("SELECT * FROM playlists WHERE id=? AND user_id=?", (pl_id, user["id"])).fetchone()
        if not pl:
            return err("Playlist not found", 404)
        body  = request.get_json(force=True) or {}
        track = body.get("track", {})
        if not track.get("videoId"):
            return err("track.videoId required", 400)
        db.execute("""INSERT OR IGNORE INTO playlist_tracks
                      (playlist_id,video_id,title,artist,thumbnail,duration)
                      VALUES (?,?,?,?,?,?)""",
                   (pl_id, track["videoId"], track.get("title"), track.get("artist"),
                    track.get("thumbnail"), track.get("duration")))
    return jsonify({"ok": True})


@app.route("/user/playlists/<int:pl_id>/remove", methods=["POST"])
@require_auth
def user_remove_from_playlist(user, pl_id):
    with get_db() as db:
        pl = db.execute("SELECT * FROM playlists WHERE id=? AND user_id=?", (pl_id, user["id"])).fetchone()
        if not pl:
            return err("Playlist not found", 404)
        vid = (request.get_json(force=True) or {}).get("videoId")
        if not vid:
            return err("videoId required", 400)
        db.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND video_id=?", (pl_id, vid))
    return jsonify({"ok": True})


@app.route("/user/playlists/<int:pl_id>", methods=["DELETE"])
@require_auth
def user_delete_playlist(user, pl_id):
    with get_db() as db:
        pl = db.execute("SELECT * FROM playlists WHERE id=? AND user_id=?", (pl_id, user["id"])).fetchone()
        if not pl:
            return err("Playlist not found", 404)
        db.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pl_id,))
        db.execute("DELETE FROM playlists WHERE id=?", (pl_id,))
    return jsonify({"ok": True})


# ─── Listen history (per-account, for recommendations) ────────────────────────

@app.route("/user/history/add", methods=["POST"])
@require_auth
def add_history(user):
    body  = request.get_json(force=True) or {}
    track = body.get("track", {})
    if not track.get("videoId"):
        return err("track.videoId required", 400)
    with get_db() as db:
        db.execute("""INSERT INTO listen_history (user_id,video_id,title,artist,thumbnail)
                      VALUES (?,?,?,?,?)""",
                   (user["id"], track["videoId"], track.get("title"),
                    track.get("artist"), track.get("thumbnail")))
        # Keep max 200 entries per user
        db.execute("""DELETE FROM listen_history WHERE user_id=? AND rowid NOT IN
                      (SELECT rowid FROM listen_history WHERE user_id=? ORDER BY played_at DESC LIMIT 200)""",
                   (user["id"], user["id"]))
    return jsonify({"ok": True})


@app.route("/user/history")
@require_auth
def get_history(user):
    with get_db() as db:
        rows = db.execute("""SELECT DISTINCT video_id,title,artist,thumbnail
                             FROM listen_history WHERE user_id=?
                             ORDER BY played_at DESC LIMIT 50""", (user["id"],)).fetchall()
    return jsonify({"history": [dict(r) for r in rows]})


@app.route("/user/recommend", methods=["POST"])
@require_auth
def user_recommend(user):
    """
    POST /user/recommend
    Uses the user's actual listen history from DB as seeds.
    Takes the most-played / most-recent tracks, calls get_watch_playlist
    for each, merges and deduplicates. Returns up to 40 tracks.
    """
    import time, random, collections

    with get_db() as db:
        rows = db.execute("""SELECT video_id, COUNT(*) as plays
                             FROM listen_history WHERE user_id=?
                             GROUP BY video_id ORDER BY plays DESC, MAX(played_at) DESC
                             LIMIT 8""", (user["id"],)).fetchall()

    seeds     = [r["video_id"] for r in rows]
    all_vids  = set(seeds)
    tracks    = []

    for vid in seeds[:5]:
        try:
            data = yt.get_watch_playlist(videoId=vid, limit=20)
            for item in data.get("tracks", []):
                item_vid = item.get("videoId")
                if not item_vid or item_vid in all_vids:
                    continue
                all_vids.add(item_vid)
                t = norm_track(item)
                if t:
                    tracks.append(t)
            time.sleep(0.05)
        except Exception as e:
            print(f"[user/recommend] seed {vid} failed: {e}")

    random.shuffle(tracks)
    return jsonify({"tracks": tracks[:40], "seedCount": len(seeds)})


# ─── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "━" * 54)
    print(f"  Muse  server v{VERSION}  — fixed charts, added /recommend")
    print(f"  http://localhost:5000")
    print("━" * 54)
    print("  Requires: pip install ytmusicapi flask flask-cors yt-dlp")
    print("  LOCAL:    open index.html in your browser")
    print("  REMOTE:   cloudflared tunnel --url http://localhost:5000")
    print("            open index.html?server=https://xxx.trycloudflare.com")
    print("━" * 54 + "\n")
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
