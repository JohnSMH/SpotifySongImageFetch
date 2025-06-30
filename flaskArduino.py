import base64, os, requests, time
from urllib.parse import urlencode
from flask import Flask, Response, redirect, request, jsonify
import threading, queue, json

app = Flask(__name__)

CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI  = "http://127.0.0.1:3000/callback"
SCOPE         = "user-read-currently-playing user-read-playback-state"
POLL_EVERY = 5          # seconds
MARKET     = "GT"
evt_q      = queue.Queue(maxsize=10)   # tiny buffer

# ─── In‑memory cache (replace with DB in prod) ─────────────────────────
spotify_tokens = {}        # { "access_token": str, "expires": epoch, "refresh": str }

# ─── 1. Kick off login ─────────────────────────────────────────────────
@app.route("/login")
def login():
    params = urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "show_dialog": "false",
    })
    return redirect(f"https://accounts.spotify.com/authorize?{params}")

# ─── 2. Callback: exchange code ↔ tokens ───────────────────────────────
@app.route("/callback")
def callback():
    code  = request.args.get("code")
    body  = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    auth_header = _basic_auth_header(CLIENT_ID, CLIENT_SECRET)
    r = requests.post("https://accounts.spotify.com/api/token",
                      data=body, headers=auth_header)
    r.raise_for_status()
    data = r.json()

    spotify_tokens["access_token"]  = data["access_token"]
    spotify_tokens["expires"]       = time.time() + data["expires_in"]   # 3600 s
    spotify_tokens["refresh"]       = data["refresh_token"]
    return "Login OK – now hit /currently‑playing"

# ─── 3. Protected endpoint ─────────────────────────────────────────────
@app.route("/currently-playing")
def currently_playing():
    _ensure_fresh_token()                   # refreshes if needed
    headers = {"Authorization": f'Bearer {spotify_tokens["access_token"]}'}
    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers=headers,
        params={"market": "GT"},
    )

    if r.status_code == 204:                # nothing playing
        return jsonify({"status": "stopped"})

    r.raise_for_status()
    item  = r.json()["item"]
    album = item["album"]
    imgs  = album.get("images", [])         # could be empty on podcasts

    # pick the smallest (last) or largest (first) or mid‑sized (index 1)
    cover_url = imgs[-1]["url"] if imgs else None    # 64×64 fallback

    return jsonify(
        track   = item["name"],
        artist  = ", ".join(a["name"] for a in item["artists"]),
        album   = album["name"],
        cover   = cover_url
    )

# ─── Helpers ───────────────────────────────────────────────────────────
def _basic_auth_header(cid, secret):
    token = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    return { "Authorization": f"Basic {token}",
             "Content-Type": "application/x-www-form-urlencoded" }

def _ensure_fresh_token():
    if time.time() < spotify_tokens.get("expires", 0) - 60:
        return                                           # still good
    body = { "grant_type": "refresh_token",
             "refresh_token": spotify_tokens["refresh"] }
    r = requests.post("https://accounts.spotify.com/api/token",
                      data=body, headers=_basic_auth_header(CLIENT_ID, CLIENT_SECRET))
    r.raise_for_status()
    data = r.json()
    spotify_tokens["access_token"] = data["access_token"]
    spotify_tokens["expires"]      = time.time() + data["expires_in"]

@app.route("/stream")
def stream():
    def gen():
        while True:
            data = evt_q.get()
            yield f"data: {json.dumps(data)}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache"})

def _poller():
    last_id = None
    while True:
        try:
            _ensure_fresh_token()
            h  = {"Authorization": f'Bearer {spotify_tokens["access_token"]}'}
            r  = requests.get("https://api.spotify.com/v1/me/player/currently-playing",
                              headers=h, params={"market": MARKET}, timeout=10)
            payload = {"status": "stopped"} if r.status_code == 204 else r.json()
            if payload != last_id:
                last_id = payload               # de‑dup
                try: evt_q.put_nowait(payload)
                except queue.Full: pass
        except Exception as e:
            print("poller err:", e)
        time.sleep(POLL_EVERY)

threading.Thread(target=_poller, daemon=True).start()

if __name__ == "__main__":
    app.run(port=3000, debug=True)