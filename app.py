# Snapchat Video Downloader Telegram bot — webhook mode for a free web host (Render).
# Env: TELEGRAM_BOT_TOKEN (required), OWNER_CHAT_ID (for /stats), PORT (host sets it),
#      PUBLIC_URL or RENDER_EXTERNAL_URL (host sets it) -> webhook is registered on startup.
import os, sys, hashlib
TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("SNAP_BOT_TOKEN") or "").strip()
OWNER_FROM_SECRET = (os.environ.get("OWNER_CHAT_ID") or "").strip()
if not TOKEN:
    print("TELEGRAM_BOT_TOKEN missing", flush=True); sys.exit(1)
import json, re, time, threading, tempfile, shutil, html, traceback, pathlib
import requests

API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
BASE = f"{API}/bot{TOKEN}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TG_LIMIT = int(float(os.environ.get("SNAP_BOT_LIMIT_MB", "49")) * 1024 * 1024)   # Telegram bot upload limit ~50 MB
MAX_PER_HOUR = 20                    # ek user 1 ghante mein max itne links
GAP_SECONDS = 10                     # do links ke beech kam se kam itna gap
MAX_STORY_ITEMS = 5                  # profile link pe max itni story videos
DATA = pathlib.Path(os.environ.get("SNAP_BOT_DATA", "/tmp/snap_bot_data.json"))

# ---------------- users / stats ----------------
_lock = threading.Lock()
def _load():
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return {"owner": None, "users": {}, "downloads": 0, "failed": 0, "started": time.time()}
db = _load()
if OWNER_FROM_SECRET:
    db["owner"] = OWNER_FROM_SECRET
def _save():
    try:
        DATA.write_text(json.dumps(db))
    except Exception:
        pass

def note_user(msg):
    u = msg.get("from") or {}
    cid = str(msg["chat"]["id"])
    with _lock:
        rec = db["users"].setdefault(cid, {"first": time.time(), "name": "", "count": 0})
        rec["name"] = (u.get("first_name") or "") + ((" @" + u["username"]) if u.get("username") else "")
        rec["last"] = time.time()
        if not db.get("owner") and msg.get("text", "").startswith("/start"):
            db["owner"] = cid
        _save()

# ---------------- telegram helpers ----------------
def tg(method, _wait=40, files=None, **params):
    try:
        if files:
            r = requests.post(f"{BASE}/{method}", data=params, files=files, timeout=_wait + 300)
        else:
            r = requests.post(f"{BASE}/{method}", json=params, timeout=_wait + 10)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error_code": r.status_code, "description": r.text[:200]}
    except requests.RequestException as e:
        return {"ok": False, "error_code": 0, "description": str(e)[:200]}

def say(chat_id, text, reply_to=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        p["reply_to_message_id"] = reply_to
        p["allow_sending_without_reply"] = True
    return tg("sendMessage", **p)

# ---------------- snapchat ----------------
LINK_RE = re.compile(r"https?://[^\s<>\"']*snapchat\.com/[^\s<>\"']*", re.I)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

class SnapError(Exception):
    def __init__(self, hinglish):
        super().__init__(hinglish); self.hinglish = hinglish

def find_link(text):
    m = LINK_RE.search(text or "")
    if m:
        return m.group(0).rstrip(").,!")
    m = re.search(r"(?:^|\s)((?:www\.|t\.)?snapchat\.com/\S+)", text or "", re.I)
    return ("https://" + m.group(1)) if m else None

def clean_variant(url):
    """Snapchat web pe '.27.' wali file mein Snapchat logo + username chalta hai.
    '.1034.' wali same video bina logo ke hoti hai (thodi kam resolution, 480p)."""
    base = url.split("?")[0]
    if ".27." in base:
        return base.replace(".27.", ".1034.")
    return None

def url_ok(url):
    try:
        r = SESSION.get(url, headers={"Range": "bytes=0-1023"}, timeout=20, stream=True)
        ok = r.status_code in (200, 206) and "video" in (r.headers.get("content-type") or "")
        r.close(); return ok
    except requests.RequestException:
        return False

def next_data(html_text):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

def items_from_page(final_url, data):
    """Page ke andar ki JSON se video/photo links nikalo."""
    pp = (data or {}).get("props", {}).get("pageProps", {}) or {}
    out = []
    # 1) Spotlight page: pehli story hi wo video hai jiska link bheja gaya
    if "/spotlight/" in final_url:
        want = final_url.split("/spotlight/")[1].split("?")[0].split("/")[0]
        feed = (pp.get("spotlightFeed") or {}).get("spotlightStories") or []
        for st in feed:
            meta = (st.get("metadata") or {})
            sid = (((st.get("story") or {}).get("storyId") or {}) or {}).get("value", "")
            link = ((meta.get("oneLinkParams") or {}).get("deepLinkUrl") or "")
            snaps = ((st.get("story") or {}).get("snapList")) or []
            if snaps and (want in link or want == sid or not out):
                u = (snaps[0].get("snapUrls") or {}).get("mediaUrl")
                vm = (meta.get("videoMetadata") or {})
                creator = (((vm.get("creator") or {}).get("personCreator") or {}).get("username")) or ""
                if u:
                    out = [{"url": u, "video": True, "title": vm.get("description") or vm.get("name") or "", "by": creator}]
                if want in link or want == sid:
                    break
        if not out:
            vm = pp.get("videoMetadata") or {}
            if vm.get("contentUrl"):
                out = [{"url": vm["contentUrl"], "video": True, "title": vm.get("description", ""), "by": ""}]
        return out
    # 2) Profile / story page: abhi ki public story
    story = pp.get("story") or {}
    for s in (story.get("snapList") or [])[-MAX_STORY_ITEMS:]:
        u = (s.get("snapUrls") or {}).get("mediaUrl")
        if u:
            out.append({"url": u, "video": s.get("snapMediaType") == 1, "title": "", "by": ""})
    return out

def ytdlp_items(url):
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(url, download=False)
        ents = info.get("entries") or [info]
        out = []
        for e in ents[:MAX_STORY_ITEMS]:
            if e and e.get("url"):
                out.append({"url": e["url"], "video": True, "title": e.get("description") or "", "by": e.get("uploader") or ""})
        return out
    except Exception:
        return []

def resolve(link):
    """Short link (t.snapchat.com / snapchat.com/t/) ko asli page tak le jao, phir media nikalo."""
    try:
        r = SESSION.get(link, timeout=25, allow_redirects=True)
    except requests.RequestException:
        raise SnapError("😕 Snapchat se connect nahi ho paya. 1-2 min baad dobara bhejo.")
    final = r.url
    if r.status_code == 404:
        raise SnapError("❌ Ye link galat hai ya video delete ho chuki hai. Snapchat mein Share → Copy Link karke dobara bhejo.")
    if r.status_code in (403, 429):
        raise SnapError("⏳ Snapchat abhi rok raha hai (bahut requests). Thodi der baad try karo.")
    items = items_from_page(final, next_data(r.text))
    if not items and "/spotlight/" in final:
        items = ytdlp_items(final)
    if not items:
        if re.search(r"snapchat\.com(:\d+)?/(add/|@)", final):
            raise SnapError("🔒 Is profile pe abhi koi <b>public</b> story nahi hai, ya account private hai. Private snaps download nahi ho sakte.")
        raise SnapError("🔒 Is link mein koi public video nahi mili. Ye private hai ya galat link hai. Sirf <b>public</b> Spotlight / public story ka link chalega.")
    return final, items

def download(url, folder, name):
    path = os.path.join(folder, name)
    with SESSION.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return path

def shrink(path):
    """50 MB se badi video ko ffmpeg se chhota karo."""
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                   capture_output=True, text=True, timeout=60).stdout.strip() or 0)
    except Exception:
        dur = 0
    if dur <= 0:
        return None
    total = int((TG_LIMIT * 8 * 0.92) / dur / 1000)      # kbps jo 50 MB mein fit ho
    akbps = 96 if total > 600 else 64
    kbps = max(120, total - akbps)
    out = path.rsplit(".", 1)[0] + "_small.mp4"
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k",
                        "-maxrate", f"{kbps}k", "-bufsize", f"{2*kbps}k", "-vf", "scale='min(720,iw)':-2", "-c:a", "aac", "-b:a", f"{akbps}k", out],
                       timeout=900, check=True)
    except Exception:
        return None
    return out if os.path.getsize(out) <= TG_LIMIT else None

# ---------------- rate limit ----------------
_hits = {}
_busy = set()
def allowed(cid):
    now = time.time()
    with _lock:
        h = [t for t in _hits.get(cid, []) if now - t < 3600]
        if cid in _busy:
            return "⏳ Pichhla video abhi aa raha hai, use aane do phir agla bhejo."
        if h and now - h[-1] < GAP_SECONDS:
            return f"⏳ Thoda ruko, {int(GAP_SECONDS - (now - h[-1])) + 1} sec baad agla link bhejo."
        if len(h) >= MAX_PER_HOUR:
            return f"🙏 1 ghante mein max {MAX_PER_HOUR} videos. Thodi der baad aao."
        h.append(now); _hits[cid] = h; _busy.add(cid)
    return None

# ---------------- handlers ----------------
WELCOME = ("👋 <b>Namaste! Main Snapchat Video Downloader bot hoon.</b>\n\n"
           "Kaise use karein:\n"
           "1️⃣ Snapchat mein video kholo → <b>Share</b> → <b>Copy Link</b>\n"
           "2️⃣ Link yahan paste karke bhejo\n"
           "3️⃣ Kuch second mein video aa jayegi ✅\n\n"
           "Chalega: public <b>Spotlight</b> videos, <b>t.snapchat.com</b> short links, public profile ki story.\n"
           "Nahi chalega: private snaps / friends-only stories.\n\n"
           "🙏 Video sirf apne personal use ke liye ya creator ki permission se download karein.")
HELP = ("ℹ️ <b>Help</b>\n"
        "• Bas Snapchat ka public link bhejo, jaise:\n<code>https://t.snapchat.com/AbCd1234</code>\n"
        "• Spotlight video aksar <b>bina Snapchat logo</b> ke milti hai. Agar kabhi logo wali hi mile to bot bata dega.\n"
        "• Profile link (snapchat.com/@naam) bhejo to us profile ki abhi ki public story (max 5) aayegi.\n"
        f"• Ek link ke baad {GAP_SECONDS} sec ruko. 1 ghante mein max {MAX_PER_HOUR}.\n"
        "• Error aaye to link dobara copy karke bhejo.")

def handle_link(msg, link):
    cid = msg["chat"]["id"]; mid = msg.get("message_id")
    wait = allowed(str(cid))
    if wait:
        say(cid, wait, mid); return
    tmp = tempfile.mkdtemp(prefix="snap_")
    status = {}
    try:
        tg("sendChatAction", chat_id=cid, action="upload_video")
        status = say(cid, "🔎 Link check ho raha hai...", mid)
        final, items = resolve(link)
        sent = 0
        for i, it in enumerate(items, 1):
            if not it["video"]:
                p = download(it["url"], tmp, f"snap_{i}.jpg")
                with open(p, "rb") as f:
                    r = tg("sendPhoto", files={"photo": f}, chat_id=cid, reply_to_message_id=mid, allow_sending_without_reply=True)
                sent += 1 if r.get("ok") else 0
                continue
            src, clean = it["url"], False
            cv = clean_variant(src)
            if cv and url_ok(cv):
                src, clean = cv, True
            elif ".1034." in src:
                clean = True
            tg("sendChatAction", chat_id=cid, action="upload_video")
            p = download(src, tmp, f"snap_{i}.mp4")
            note = ""
            if os.path.getsize(p) > TG_LIMIT:
                small = shrink(p)
                if small:
                    p, note = small, "\n(50 MB se badi thi, isliye thodi compress ki)"
                else:
                    say(cid, "📦 Video 50 MB se badi hai, Telegram pe nahi jaa sakti. Ye direct link kholo (thodi der hi chalega):\n" + html.escape(src), mid)
                    sent += 1
                    continue
            cap = "✅ Bina Snapchat logo ke" if clean else "✅ Video (isme Snapchat ka logo hai — bina logo wali copy nahi mili)"
            if it.get("by"):
                cap += f"\n👤 @{it['by']}"
            cap += note
            with open(p, "rb") as f:
                r = tg("sendVideo", files={"video": (f"snapchat_{i}.mp4", f, "video/mp4")}, chat_id=cid, caption=cap[:1000],
                       supports_streaming="true", reply_to_message_id=mid, allow_sending_without_reply="true")
            if r.get("ok"):
                sent += 1
            else:
                with open(p, "rb") as f:   # video ki jagah file bhej ke dekho
                    r = tg("sendDocument", files={"document": (f"snapchat_{i}.mp4", f, "video/mp4")}, chat_id=cid, caption=cap[:1000])
                sent += 1 if r.get("ok") else 0
        with _lock:
            db["downloads"] += sent
            rec = db["users"].get(str(cid))
            if rec: rec["count"] = rec.get("count", 0) + sent
            _save()
        if status.get("ok"):
            tg("deleteMessage", chat_id=cid, message_id=status["result"]["message_id"])
        if sent == 0:
            say(cid, "😕 Video bhej nahi paya. 1 min baad dobara try karo.", mid)
    except SnapError as e:
        with _lock: db["failed"] += 1; _save()
        if status.get("ok"):
            tg("deleteMessage", chat_id=cid, message_id=status["result"]["message_id"])
        say(cid, e.hinglish, mid)
    except Exception as e:
        with _lock: db["failed"] += 1; _save()
        print("⚠️ error:", repr(e)); traceback.print_exc()
        say(cid, "😕 Kuch gadbad ho gayi. Link dobara copy karke bhejo, ya thodi der baad try karo.", mid)
    finally:
        with _lock: _busy.discard(str(cid))
        shutil.rmtree(tmp, ignore_errors=True)

def stats_text():
    now = time.time()
    us = db["users"]
    d1 = sum(1 for u in us.values() if now - u.get("last", 0) < 86400)
    d30 = sum(1 for u in us.values() if now - u.get("last", 0) < 30 * 86400)
    new1 = sum(1 for u in us.values() if now - u.get("first", 0) < 86400)
    return (f"📊 <b>Stats</b>\nTotal users: <b>{len(us)}</b>\nAaj active: {d1} (naye: {new1})\nPichhle 30 din active: {d30}\n"
            f"Videos bheji: {db['downloads']}\nFail: {db['failed']}\n\n(Server so jaane / restart pe count 0 se shuru hota hai.)")

def on_message(msg):
    if "chat" not in msg:
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    cid = msg["chat"]["id"]
    note_user(msg)
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd == "/start":
        say(cid, WELCOME); return
    if cmd == "/help":
        say(cid, HELP); return
    if cmd == "/stats":
        if str(cid) == str(db.get("owner")):
            say(cid, stats_text())
        else:
            say(cid, "Ye command sirf bot owner ke liye hai 🙂")
        return
    link = find_link(text)
    if link:
        threading.Thread(target=handle_link, args=(msg, link), daemon=True).start()
    elif msg["chat"].get("type") == "private":
        say(cid, "🔗 Snapchat ka <b>link</b> bhejo (Share → Copy Link). Madad ke liye /help")


# ---------------- webhook server ----------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
SECRET = hashlib.sha256(("snapbot:" + TOKEN).encode()).hexdigest()[:48]
HOOK_PATH = "/tg/" + SECRET[:24]
PUBLIC = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
_seen = []

def process_update(upd):
    uid = upd.get("update_id")
    with _lock:
        if uid in _seen:
            return
        _seen.append(uid); del _seen[:-500]
    msg = upd.get("message")
    if msg:
        t = (msg.get("text") or "")
        print("update:", "command" if t.startswith("/") else ("link" if find_link(t) else "other"), flush=True)
        try:
            on_message(msg)
        except Exception as e:
            print("handler error:", repr(e), flush=True)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _ok(self, body=b"ok", code=200):
        self.send_response(code); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/diag/") and self.path.split("?")[0] == "/diag/" + SECRET:
            return self._ok(json.dumps(diag(self.path.split("u=", 1)[1] if "u=" in self.path else "")).encode())
        self._ok(b"Snap bot is running")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path != HOOK_PATH or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            return self._ok(b"no", 403)
        try:
            upd = json.loads(raw or b"{}")
        except Exception:
            return self._ok()
        self._ok()   # Telegram ko turant 200, kaam background mein
        threading.Thread(target=process_update, args=(upd,), daemon=True).start()

def diag(link):
    # owner-only check (secret path): kya Snapchat is server ki IP se video deta hai?
    from urllib.parse import unquote
    out = {"link": unquote(link)}
    try:
        final, items = resolve(unquote(link))
        out["items"] = len(items)
        if items:
            src = items[0]["url"]; cv = clean_variant(src)
            out["clean_available"] = bool(cv and url_ok(cv)) or ".1034." in src
            r = SESSION.get(cv if out["clean_available"] and cv else src, stream=True, timeout=30)
            n = 0
            for ch in r.iter_content(1 << 16):
                n += len(ch)
                if n > 3_000_000: break
            out["status"] = r.status_code; out["bytes_read"] = n; out["ctype"] = r.headers.get("content-type"); r.close()
    except SnapError as e:
        out["snap_error"] = e.hinglish
    except Exception as e:
        out["error"] = repr(e)[:200]
    return out

def register_webhook():
    if not PUBLIC:
        print("PUBLIC_URL/RENDER_EXTERNAL_URL not set; webhook not registered", flush=True); return
    for attempt in range(5):
        r = tg("setWebhook", url=PUBLIC + HOOK_PATH, secret_token=SECRET, allowed_updates=["message"],
               drop_pending_updates=False, max_connections=20)
        if r.get("ok"):
            print("webhook set", flush=True); break
        print("setWebhook failed:", r.get("error_code"), r.get("description"), flush=True)
        time.sleep(3)
    tg("setMyCommands", commands=[{"command": "start", "description": "Shuru karo"}, {"command": "help", "description": "Kaise use karein"}])

def webhook_guard():
    # Agar koi purana Colab copy webhook hata de, to wapas laga do (jab tak server jaag raha hai)
    while True:
        time.sleep(600)
        try:
            info = tg("getWebhookInfo").get("result") or {}
            if PUBLIC and info.get("url") != PUBLIC + HOOK_PATH:
                print("webhook missing, re-registering", flush=True); register_webhook()
        except Exception:
            pass

if __name__ == "__main__":
    me = tg("getMe")
    print("getMe ok:", me.get("ok"), (me.get("result") or {}).get("username"), me.get("error_code"), flush=True)
    port = int(os.environ.get("PORT", "10000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=register_webhook, daemon=True).start()
    threading.Thread(target=webhook_guard, daemon=True).start()
    print("listening on", port, flush=True)
    srv.serve_forever()
