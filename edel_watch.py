#!/usr/bin/env python3
"""
Watcher channel Telegram — auto-tangkap REGISTRATION ACCESS CODE yang di-share
developer di channel/grup, lalu jalankan register otomatis (register_http.py).

Beda dgn supa (invite code per-akun): di Edel, developer broadcast SATU access
code. Server /auth/register/start skarang wajib field `accessCode` — tanpa itu
403 REGISTRATION_ACCESS_CODE_REQUIRED, salah → REGISTRATION_ACCESS_CODE_INVALID.
Watcher ini nangkep kode itu begitu diposting → register akun sebanyak target.

Pakai Telethon (MTProto, login AKUN kamu sendiri) — bisa baca channel/grup apapun
yang kamu ikuti TANPA jadi admin & tanpa nambah bot. Login HP sekali (watch.session).

Alur:
  1. Login akun Telegram (first run: nomor HP + OTP telegram).
  2. Listen pesan baru di EDEL_TG_WATCH (push handler + poller getHistory).
  3. Tiap pesan: regex ambil access code (default: token setelah label
     "access code/kode/registration code:"; atau set EDEL_CODE_RE sendiri).
  4. Kode baru → PROBE dulu (register/start email buangan, tak di-finish = tak
     bikin akun) → kalau valid → register_http.register_until / register_batch.
  5. Hasil dikirim ke bot alert (TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID) / Telethon 'me'.

env (.env):
  EDEL_TG_API_ID    api_id  dari https://my.telegram.org  (wajib)
  EDEL_TG_API_HASH  api_hash dari https://my.telegram.org (wajib)
  EDEL_TG_WATCH     channel/grup dipantau (wajib). @user, id, atau link.
  EDEL_TG_NOTIFY    tujuan notif kalau tanpa bot (default: me = Saved Messages).
  EDEL_TG_ALLOW     (tak dipakai di watcher) — lihat notif via bot di bawah.
  EDEL_WATCH_TARGET N → register SAMPAI total akun >= N (register_until). default 0.
  EDEL_WATCH_BATCH  n → kalau TARGET 0: register n akun BARU tiap kode baru. default 5.
  REG_WORKERS       paralel register (default 4). REG_JITTER jeda antar worker.
  EDEL_CODE_RE      regex custom akses-kode (group 1 = kode). Kosong = heuristik label.
  EDEL_TG_CATCHUP   scan N pesan terakhir saat start (default 0).
  EDEL_TG_POLL      detik antar poll getHistory (default 1.0; 0=off).
  EDEL_TG_POLL_LIMIT N pesan terakhir tiap poll (default 3).
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  → notif via bot (andal). Kosong = Telethon.

jalan:
  python3 edel_watch.py                    # daemon (login first-run kalau belum)
  python3 edel_watch.py --catchup 20       # proses 20 pesan terakhir dulu, lalu listen
  python3 edel_watch.py --test "Access code: ABCD-1234"   # tes regex extract (offline)
  python3 edel_watch.py --code ABCD-1234   # fire register manual sekali (tanpa telegram)
"""
import os, sys, re, json, asyncio, functools

BASEDIR = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv(path):
    """Muat KEY=VALUE dari .env ke os.environ (tanpa override yang sudah di-set). Tanpa dependency."""
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
_load_dotenv(os.environ.get("EDEL_ENV") or os.path.join(BASEDIR, ".env"))

import requests
import register_http as RH  # register_batch / register_until / REG_ACCESS_CODE / BASE / ORIGIN

try:
    from telethon import TelegramClient, events
except ImportError:
    print("Telethon belum ada. Install: pip3 install telethon"); sys.exit(1)

API_ID     = os.environ.get("EDEL_TG_API_ID", "").strip()
API_HASH   = os.environ.get("EDEL_TG_API_HASH", "").strip()
WATCH      = os.environ.get("EDEL_TG_WATCH", "").strip()
NOTIFY     = os.environ.get("EDEL_TG_NOTIFY", "me").strip() or "me"
TARGET     = int(os.environ.get("EDEL_WATCH_TARGET", "0") or "0")
BATCH      = int(os.environ.get("EDEL_WATCH_BATCH", "5") or "5")
WORKERS    = int(os.environ.get("REG_WORKERS", "4") or "4")
CATCHUP    = int(os.environ.get("EDEL_TG_CATCHUP", "0") or "0")
POLL       = float(os.environ.get("EDEL_TG_POLL", "1.0") or "1.0")
POLL_LIMIT = int(os.environ.get("EDEL_TG_POLL_LIMIT", "3") or "3")
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_CHAT   = (os.environ.get("TELEGRAM_CHAT_ID", "").strip()
              or (NOTIFY if NOTIFY.lstrip("-").isdigit() else ""))

SESSION   = os.path.join(BASEDIR, "watch.session")
SEEN_FILE = os.path.join(BASEDIR, "watch_seen.json")

# Access code Edel formatnya bebas (server cuma cek valid/invalid). Default: tangkap
# token SETELAH label ("access code / registration code / kode akses ...:"). Kalau
# format dev beda, set EDEL_CODE_RE (group 1 = kode).
_CUSTOM_RE = os.environ.get("EDEL_CODE_RE", "").strip()
RE_LABELLED = re.compile(
    r"(?:access[\s_-]*code|reg(?:istration)?[\s_-]*code|kode[\s_-]*(?:akses|register|registrasi)?)"
    r"\s*[:=]?\s*[`\"']?([A-Za-z0-9][A-Za-z0-9._-]{3,63})[`\"']?",
    re.IGNORECASE)
RE_CUSTOM = re.compile(_CUSTOM_RE) if _CUSTOM_RE else None

_seen = set()   # dedup access code sudah diproses


def _load_seen():
    global _seen
    try:
        with open(SEEN_FILE) as f:
            _seen = set(json.load(f))
    except (FileNotFoundError, ValueError):
        _seen = set()


def _save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(_seen), f)
    except OSError:
        pass


def extract_codes(text):
    """Ambil access code dari teks. EDEL_CODE_RE (group 1) kalau di-set; else heuristik
    label. Dedup, urut kemunculan."""
    if not text:
        return []
    codes, seen = [], set()
    rx = RE_CUSTOM or RE_LABELLED
    for m in rx.finditer(text):
        c = (m.group(1) if m.groups() else m.group(0)).strip()
        if c and c.lower() not in ("required", "invalid") and c not in seen:
            seen.add(c); codes.append(c)
    return codes


def probe_code(code):
    """Cek kode valid TANPA bikin akun: register/start email buangan + accessCode, TAK
    di-finish. Return (ok, why). Header browser wajib (edge WAF 404 kalau UA non-browser)."""
    email = f"probe_{RH.secrets.token_hex(5)}@weling.web.id"
    try:
        r = requests.post(RH.BASE + "/auth/register/start",
            json={"email": email, "displayName": "probe", "accessCode": code},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "Mozilla/5.0", "Origin": RH.ORIGIN,
                     "Referer": RH.ORIGIN + "/register"},
            timeout=25)
    except Exception as e:
        return None, f"probe err: {e}"   # None = tak yakin, tetap coba register
    if r.status_code == 200:
        return True, "valid"
    try:
        code_err = r.json().get("error", {}).get("code", "") or f"HTTP {r.status_code}"
    except Exception:
        code_err = f"HTTP {r.status_code}"
    return False, code_err


def _run_register_blocking(code):
    """Blocking — dipanggil lewat executor. Set access code lalu register."""
    RH.REG_ACCESS_CODE = code   # register_one baca global ini saat call
    log = lambda m: print("[reg]", m)
    if TARGET > 0:
        made = RH.register_until(TARGET, workers=WORKERS, log=log)
        return {"mode": "until", "target": TARGET, "made": made, "total": RH._count_accts()}
    made = RH.register_batch(BATCH, workers=WORKERS, log=log)
    return {"mode": "batch", "batch": BATCH, "made": made, "total": RH._count_accts()}


async def process(client, codes, src="", force=False):
    """Proses access code baru: dedup, probe, register di executor, notif."""
    fresh = list(codes) if force else [c for c in codes if c not in _seen]
    if not fresh:
        return
    for c in fresh:
        _seen.add(c)
    _save_seen()
    for code in fresh:
        ok, why = probe_code(code)
        if ok is False:
            await notify(client, f"⛔ Kode <code>{code}</code> dari <b>{src}</b> ditolak: {why} — skip.")
            continue
        tag = "valid ✅" if ok else f"tak yakin ({why}) — tetap coba"
        await notify(client, f"🔔 Access code baru dari <b>{src}</b>: <code>{code}</code> ({tag}) → register…")
        try:
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, functools.partial(_run_register_blocking, code))
        except Exception as e:
            await notify(client, f"❌ error register: {e}")
            continue
        await notify(client, f"🏁 Selesai (mode {res['mode']}): +{res['made']} akun baru. "
                             f"Total akun: {res['total']}.")


async def poller(client, title):
    """Poll getHistory tiap POLL detik — deteksi kode tanpa nunggu push MTProto (bisa telat)."""
    if POLL <= 0:
        return
    from telethon.errors import FloodWaitError
    print(f"poller aktif: getHistory tiap {POLL}s (limit {POLL_LIMIT}).")
    while True:
        try:
            msgs = await client.get_messages(WATCH, limit=POLL_LIMIT)
            codes = []
            for m in reversed(msgs or []):
                codes += extract_codes(m.message or "")
            if codes:
                await process(client, codes, src=title + " (poll)")
        except FloodWaitError as e:
            print(f"poll FloodWait {e.seconds}s — mundur"); await asyncio.sleep(e.seconds + 1); continue
        except Exception as e:
            print("poll err:", e)
        await asyncio.sleep(POLL)


def _tg_bot_send(text):
    if not (BOT_TOKEN and BOT_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": BOT_CHAT, "text": text[:4000], "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        print("tg bot notify err:", e)


async def notify(client, text):
    print(re.sub("<[^>]+>", "", text))
    if BOT_TOKEN and BOT_CHAT:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _tg_bot_send, text)
        return
    try:
        await client.send_message(NOTIFY, text, parse_mode="html")
    except Exception as e:
        print("notify err:", e)


async def catchup(client):
    if CATCHUP <= 0:
        return
    print(f"catchup: scan {CATCHUP} pesan terakhir @ {WATCH}…")
    all_codes = []
    async for msg in client.iter_messages(WATCH, limit=CATCHUP):
        all_codes += extract_codes(msg.message or "")
    all_codes = list(dict.fromkeys(all_codes))
    new = [c for c in all_codes if c not in _seen]
    print(f"catchup: {len(all_codes)} kode ({len(new)} baru).")
    if new:
        await process(client, new, src=f"{WATCH} (catchup)")


async def amain():
    if not API_ID or not API_HASH:
        print("Set EDEL_TG_API_ID & EDEL_TG_API_HASH di .env (https://my.telegram.org)."); sys.exit(1)
    if not WATCH:
        print("Set EDEL_TG_WATCH (channel/grup yang dipantau)."); sys.exit(1)
    _load_seen()
    client = TelegramClient(SESSION, int(API_ID), API_HASH)
    await client.start()  # first run: nomor HP + OTP telegram (interaktif)
    me = await client.get_me()
    ent = await client.get_entity(WATCH)
    title = getattr(ent, "title", None) or getattr(ent, "username", WATCH)
    mode = f"until {TARGET}" if TARGET > 0 else f"batch {BATCH}/kode"
    print(f"login sbg {me.first_name} (id {me.id}). Pantau: {title}. Mode {mode}. "
          f"Notif → {'bot' if (BOT_TOKEN and BOT_CHAT) else NOTIFY}")
    await notify(client, f"👀 Edel watcher aktif. Pantau <b>{title}</b> ({mode}). "
                         f"Access code auto-register.")

    @client.on(events.NewMessage(chats=WATCH))
    async def _(event):
        codes = extract_codes(event.message.message or "")
        if codes:
            await process(client, codes, src=title)

    await catchup(client)
    print("listening (push handler + poller)… (Ctrl-C keluar)")
    ptask = asyncio.create_task(poller(client, title))
    try:
        await client.run_until_disconnected()
    finally:
        ptask.cancel()


def _cli_test(text):
    codes = extract_codes(text)
    print("extracted:", codes or "(tak ada — set EDEL_CODE_RE atau cek format)")


def _cli_code(code):
    """Fire register manual sekali (tanpa telegram)."""
    ok, why = probe_code(code)
    print(f"probe: ok={ok} why={why}")
    if ok is False:
        print("kode ditolak, batal."); return
    res = _run_register_blocking(code)
    print("hasil:", res)


if __name__ == "__main__":
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        _cli_test(sys.argv[i + 1] if i + 1 < len(sys.argv) else ""); sys.exit(0)
    if "--code" in sys.argv:
        i = sys.argv.index("--code")
        if i + 1 >= len(sys.argv):
            print("usage: --code <ACCESSCODE>"); sys.exit(1)
        _cli_code(sys.argv[i + 1]); sys.exit(0)
    if "--catchup" in sys.argv:
        i = sys.argv.index("--catchup")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            CATCHUP = int(sys.argv[i + 1])
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\ndihentikan.")
