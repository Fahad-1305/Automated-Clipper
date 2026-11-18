"""ClipBot dashboard: FastAPI backend. Run: uvicorn app:app --host 127.0.0.1 --port 8000"""
import json, secrets, sqlite3, subprocess, sys, threading, time, datetime as dt
from pathlib import Path
from urllib.parse import urlencode
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from apscheduler.schedulers.background import BackgroundScheduler

ROOT = Path(__file__).parent
DB = ROOT / "clipbot.db"
OUT = ROOT / "out"; OUT.mkdir(exist_ok=True)
BASE = "http://localhost:8000"
G = "https://graph.facebook.com/v21.0"
GV = "https://graph-video.facebook.com/v21.0"

DEFAULTS = {
    "game_name": "Grand Theft Auto VI", "lookback_hours": 6, "min_views": 50, "max_seconds": 60,
    "max_posts_per_run": 3, "hashtags": "#GTA6 #GTAVI #gaming",
    "post_youtube": True, "post_facebook": True, "post_instagram": True,
    "youtube_privacy": "private", "auto_run": False, "interval_minutes": 60,
}

# ---------- storage ----------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS streamers(login TEXT PRIMARY KEY, name TEXT, avatar TEXT, permission INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS posted(clip_id TEXT PRIMARY KEY, streamer TEXT, title TEXT, yt TEXT, fb TEXT, ig TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, msg TEXT);""")
    return c

def kv_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default

def kv_set(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))

def settings():
    return {**DEFAULTS, **kv_get("settings", {})}

def log(msg):
    print(msg)
    with db() as c:
        c.execute("INSERT INTO log(ts,msg) VALUES(?,?)", (dt.datetime.now().strftime("%m-%d %H:%M:%S"), msg))

# ---------- app ----------
app = FastAPI()
sched = BackgroundScheduler()
run_lock = threading.Lock()
oauth_states = set()

@app.on_event("startup")
def _start():
    sched.start(); reschedule()

def reschedule():
    s = settings()
    if sched.get_job("clip"):
        sched.remove_job("clip")
    if s["auto_run"]:
        sched.add_job(run_job, "interval", minutes=max(5, int(s["interval_minutes"])), id="clip")

@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")

# ----- credentials / status -----
CRED_KEYS = ["twitch_client_id", "twitch_client_secret", "google_client_id", "google_client_secret",
             "meta_app_id", "meta_app_secret"]

@app.get("/api/status")
def status():
    cr, meta, yt = kv_get("creds", {}), kv_get("meta", {}), kv_get("youtube", {})
    return {
        "creds_set": {k: bool(cr.get(k)) for k in CRED_KEYS},
        "youtube_connected": bool(yt.get("refresh_token")),
        "meta_connected": bool(meta.get("user_token")),
        "pages": [{"id": p["id"], "name": p["name"], "ig": (p.get("instagram_business_account") or {}).get("username")}
                  for p in meta.get("pages", [])],
        "selected_page": meta.get("page_id"), "ig_username": meta.get("ig_username"),
        "redirects": {"google": f"{BASE}/auth/google/callback", "meta": f"{BASE}/auth/meta/callback"},
        "running": run_lock.locked(),
    }

@app.put("/api/credentials")
async def put_creds(req: Request):
    body = await req.json()
    cr = kv_get("creds", {})
    cr.update({k: v.strip() for k, v in body.items() if k in CRED_KEYS and v.strip()})
    kv_set("creds", cr)
    return {"ok": True}

@app.get("/api/settings")
def get_settings(): return settings()

@app.put("/api/settings")
async def put_settings(req: Request):
    body = await req.json()
    kv_set("settings", {k: body[k] for k in DEFAULTS if k in body})
    reschedule()
    return settings()

# ----- streamers -----
def twitch_headers():
    cr = kv_get("creds", {})
    if not (cr.get("twitch_client_id") and cr.get("twitch_client_secret")):
        raise HTTPException(400, "Add Twitch credentials in Accounts first")
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": cr["twitch_client_id"], "client_secret": cr["twitch_client_secret"],
        "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return {"Client-Id": cr["twitch_client_id"], "Authorization": f"Bearer {r.json()['access_token']}"}

@app.get("/api/streamers")
def list_streamers():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM streamers ORDER BY login")]

@app.post("/api/streamers")
async def add_streamer(req: Request):
    body = await req.json()
    login = body["login"].strip().lower().lstrip("@")
    u = requests.get("https://api.twitch.tv/helix/users", headers=twitch_headers(),
                     params={"login": login}, timeout=30).json().get("data", [])
    if not u:
        raise HTTPException(404, f"No Twitch user '{login}'")
    with db() as c:
        c.execute("INSERT OR REPLACE INTO streamers VALUES(?,?,?,?)",
                  (login, u[0]["display_name"], u[0]["profile_image_url"], int(bool(body.get("permission")))))
    return {"ok": True}

@app.patch("/api/streamers/{login}")
async def patch_streamer(login: str, req: Request):
    body = await req.json()
    with db() as c:
        c.execute("UPDATE streamers SET permission=? WHERE login=?", (int(bool(body["permission"])), login))
    return {"ok": True}

@app.delete("/api/streamers/{login}")
def del_streamer(login: str):
    with db() as c:
        c.execute("DELETE FROM streamers WHERE login=?", (login,))
    return {"ok": True}

# ----- Google / YouTube OAuth -----
@app.get("/auth/google")
def google_login():
    cr = kv_get("creds", {})
    if not cr.get("google_client_id"):
        raise HTTPException(400, "Add Google credentials first")
    st = secrets.token_urlsafe(16); oauth_states.add(st)
    q = urlencode({"client_id": cr["google_client_id"], "redirect_uri": f"{BASE}/auth/google/callback",
                   "response_type": "code", "scope": "https://www.googleapis.com/auth/youtube.upload",
                   "access_type": "offline", "prompt": "consent", "state": st})
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{q}")

@app.get("/auth/google/callback")
def google_cb(code: str, state: str):
    if state not in oauth_states: raise HTTPException(400, "bad state")
    oauth_states.discard(state)
    cr = kv_get("creds", {})
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cr["google_client_id"], "client_secret": cr["google_client_secret"],
        "redirect_uri": f"{BASE}/auth/google/callback", "grant_type": "authorization_code"}, timeout=30)
    r.raise_for_status()
    kv_set("youtube", {"refresh_token": r.json()["refresh_token"]})
    log("YouTube connected")
    return RedirectResponse("/#accounts")

# ----- Meta (Facebook Page + Instagram) OAuth -----
@app.get("/auth/meta")
def meta_login():
    cr = kv_get("creds", {})
    if not cr.get("meta_app_id"):
        raise HTTPException(400, "Add Meta credentials first")
    st = secrets.token_urlsafe(16); oauth_states.add(st)
    q = urlencode({"client_id": cr["meta_app_id"], "redirect_uri": f"{BASE}/auth/meta/callback", "state": st,
                   "scope": "pages_show_list,pages_manage_posts,pages_read_engagement,instagram_basic,"
                            "instagram_content_publish,business_management"})
    return RedirectResponse(f"https://www.facebook.com/v21.0/dialog/oauth?{q}")

@app.get("/auth/meta/callback")
def meta_cb(code: str, state: str):
    if state not in oauth_states: raise HTTPException(400, "bad state")
    oauth_states.discard(state)
    cr = kv_get("creds", {})
    base = {"client_id": cr["meta_app_id"], "client_secret": cr["meta_app_secret"]}
    t = requests.get(f"{G}/oauth/access_token", params={**base, "code": code,
                     "redirect_uri": f"{BASE}/auth/meta/callback"}, timeout=30).json()
    ll = requests.get(f"{G}/oauth/access_token", params={**base, "grant_type": "fb_exchange_token",
                      "fb_exchange_token": t["access_token"]}, timeout=30).json()
    tok = ll["access_token"]
    pages = requests.get(f"{G}/me/accounts", params={
        "fields": "name,access_token,instagram_business_account{id,username}", "access_token": tok},
        timeout=30).json().get("data", [])
    meta = {"user_token": tok, "pages": pages}
    if len(pages) == 1:
        meta.update(_pick(pages[0]))
    kv_set("meta", meta)
    log("Facebook/Instagram connected")
    return RedirectResponse("/#accounts")

def _pick(p):
    ig = p.get("instagram_business_account") or {}
    return {"page_id": p["id"], "page_token": p["access_token"], "ig_id": ig.get("id"),
            "ig_username": ig.get("username")}

@app.post("/api/meta/select")
async def meta_select(req: Request):
    pid = (await req.json())["page_id"]
    meta = kv_get("meta", {})
    p = next((x for x in meta.get("pages", []) if x["id"] == pid), None)
    if not p: raise HTTPException(404)
    meta.update(_pick(p)); kv_set("meta", meta)
    return {"ok": True}

@app.post("/api/disconnect/{which}")
def disconnect(which: str):
    if which in ("youtube", "meta"):
        kv_set(which, {})
    return {"ok": True}

# ----- pipeline -----
def fetch_clips(s):
    h = twitch_headers()
    with db() as c:
        logins = [r["login"] for r in c.execute("SELECT login FROM streamers WHERE permission=1")]
        done = {r["clip_id"] for r in c.execute("SELECT clip_id FROM posted")}
    if not logins:
        log("No streamers with permission confirmed"); return []
    since = (dt.datetime.utcnow() - dt.timedelta(hours=int(s["lookback_hours"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    users = requests.get("https://api.twitch.tv/helix/users", headers=h,
                         params=[("login", l) for l in logins], timeout=30).json()["data"]
    gid = None
    if s["game_name"]:
        g = requests.get("https://api.twitch.tv/helix/games", headers=h, params={"name": s["game_name"]}, timeout=30).json()["data"]
        gid = g[0]["id"] if g else None
    clips = []
    for u in users:
        p = {"broadcaster_id": u["id"], "started_at": since, "first": 20}
        if gid: p["game_id"] = gid
        for c in requests.get("https://api.twitch.tv/helix/clips", headers=h, params=p, timeout=30).json()["data"]:
            if c["id"] not in done and c["view_count"] >= int(s["min_views"]) and c["duration"] <= int(s["max_seconds"]):
                clips.append(c)
    clips.sort(key=lambda c: c["view_count"], reverse=True)
    return clips[: int(s["max_posts_per_run"])]

def make_vertical(c):
    raw, out = OUT / f"{c['id']}_raw.mp4", OUT / f"{c['id']}.mp4"
    subprocess.run([sys.executable, "-m", "yt_dlp", "-o", str(raw), "-f", "best[ext=mp4]/best", c["url"]], check=True, capture_output=True)
    vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
          "[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-filter_complex", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-c:a", "aac", "-b:a", "128k", str(out)], check=True, capture_output=True)
    return out

def post_youtube(path, title, desc, privacy):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    cr = kv_get("creds", {})
    creds = Credentials(None, refresh_token=kv_get("youtube")["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=cr["google_client_id"], client_secret=cr["google_client_secret"])
    yt = build("youtube", "v3", credentials=creds)
    req = yt.videos().insert(part="snippet,status", body={
        "snippet": {"title": title, "description": desc, "categoryId": "20"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}},
        media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]

def post_facebook(path, title, desc):
    m = kv_get("meta")
    with open(path, "rb") as f:
        r = requests.post(f"{GV}/{m['page_id']}/videos", data={"access_token": m["page_token"], "title": title,
                          "description": desc}, files={"source": f}, timeout=600)
    r.raise_for_status()
    return r.json()["id"]

def post_instagram(path, caption):
    m = kv_get("meta")
    if not m.get("ig_id"):
        raise RuntimeError("Selected Page has no linked Instagram Business account")
    tok = m["page_token"]
    r = requests.post(f"{G}/{m['ig_id']}/media", data={"media_type": "REELS", "upload_type": "resumable",
                      "caption": caption, "access_token": tok}, timeout=60)
    r.raise_for_status()
    cid = r.json()["id"]
    uri = r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/v21.0/{cid}"
    with open(path, "rb") as f:
        requests.post(uri, headers={"Authorization": f"OAuth {tok}", "offset": "0",
                      "file_size": str(path.stat().st_size),
                      "Content-Type": "application/octet-stream"}, data=f, timeout=600).raise_for_status()
    for _ in range(60):
        st = requests.get(f"{G}/{cid}", params={"fields": "status_code", "access_token": tok}, timeout=30).json()
        if st.get("status_code") == "FINISHED": break
        if st.get("status_code") == "ERROR": raise RuntimeError("Instagram failed to process video")
        time.sleep(5)
    p = requests.post(f"{G}/{m['ig_id']}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60)
    p.raise_for_status()
    return p.json()["id"]

def run_job():
    if not run_lock.acquire(blocking=False):
        return
    try:
        s = settings()
        log("Run started")
        clips = fetch_clips(s)
        log(f"{len(clips)} new clip(s)")
        for c in clips:
            ids = {"yt": None, "fb": None, "ig": None}
            try:
                vid = make_vertical(c)
                name = c["broadcaster_name"]
                title = f"{c['title']} | {name} #GTA6 #Shorts"[:100]
                desc = (f"Clip from {name} - watch live: https://twitch.tv/{c['broadcaster_login']}\n"
                        f"Original clip: {c['url']}\n{s['hashtags']}")
                if s["post_youtube"] and kv_get("youtube", {}).get("refresh_token"):
                    ids["yt"] = post_youtube(vid, title, desc, s["youtube_privacy"])
                if s["post_facebook"] and kv_get("meta", {}).get("page_id"):
                    ids["fb"] = post_facebook(vid, title, desc)
                if s["post_instagram"] and kv_get("meta", {}).get("ig_id"):
                    ids["ig"] = post_instagram(vid, f"{c['title']}\nClip from {name} (twitch.tv/{c['broadcaster_login']})\n{s['hashtags']}")
                with db() as d:
                    d.execute("INSERT OR REPLACE INTO posted VALUES(?,?,?,?,?,?,?)",
                              (c["id"], name, c["title"], ids["yt"], ids["fb"], ids["ig"], dt.datetime.now().isoformat(timespec="minutes")))
                log(f"Posted '{c['title']}' ({name}) -> " + ", ".join(k.upper() for k, v in ids.items() if v))
            except Exception as e:
                log(f"FAILED {c['id']}: {e}")
            finally:
                for f in OUT.glob(f"{c['id']}*"): f.unlink(missing_ok=True)
        log("Run finished")
    except Exception as e:
        log(f"Run error: {e}")
    finally:
        run_lock.release()

@app.post("/api/run")
def run_now():
    if run_lock.locked():
        raise HTTPException(409, "Already running")
    threading.Thread(target=run_job, daemon=True).start()
    return {"started": True}

@app.get("/api/log")
def get_log():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT ts,msg FROM log ORDER BY id DESC LIMIT 100")]

@app.get("/api/posted")
def get_posted():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM posted ORDER BY ts DESC LIMIT 50")]
