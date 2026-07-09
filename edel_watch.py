#!/usr/bin/env python3
"""
Watcher channel Telegram — auto-tangkap ACCESS CODE register yang di-share developer
di channel/grup, lalu register akun otomatis (register_http.py). Satu baris = satu kode.

Server /auth/register/start skarang wajib field `accessCode`: tanpa itu 403
REGISTRATION_ACCESS_CODE_REQUIRED, salah → REGISTRATION_ACCESS_CODE_INVALID.
Developer broadcast DAFTAR kode (1 per baris) → watcher register 1 akun per kode.

Format kode yang dikenali (1 per baris):
  edel::9feb933d-e00e-413c-9989-59cda0c71f33   (prefix::uuid — dikirim apa adanya;
                                                kalau ditolak, coba lagi tanpa prefix)
  9feb933d-e00e-413c-...                        (uuid telanjang)
  VZR57MP7P8                                    (kode pendek [A-Z0-9], huruf+angka)
Atau set EDEL_CODE_RE (regex, group 1 = kode) buat format lain.

Pakai Telethon (MTProto, login AKUN kamu sendiri) — baca channel/grup apapun yang
kamu ikuti TANPA jadi admin. Login HP sekali (watch.session), lalu headless.

env (.env):
  EDEL_TG_API_ID    api_id  dari https://my.telegram.org  (wajib)
  EDEL_TG_API_HASH  api_hash dari https://my.telegram.org (wajib)
  EDEL_TG_WATCH     channel/grup dipantau, PISAH KOMA buat multi (default:
                    handlpay,oneswap_community,edeldotfinance). @user, id, atau link.
  EDEL_TG_NOTIFY    tujuan notif kalau tanpa bot (default: me = Saved Messages).
  EDEL_CODE_USES    berapa akun di-register per kode (default 1; naikkan kalau 1 kode multi-pakai).
  EDEL_PROBE        1 = cek kode dulu (register/start, tak finish) sebelum register.
                    default 0 (kode single-use bisa "kepakai" oleh probe → matikan).
  REG_WORKERS       paralel register (default 4). REG_JITTER jeda antar worker.
  EDEL_CODE_RE      regex custom akses-kode (group 1 = kode). Kosong = 3 pola bawaan.
  EDEL_TG_CATCHUP   scan N pesan terakhir saat start (default 0).
  EDEL_TG_POLL      detik antar poll getHistory (default 1.0; 0=off).
  EDEL_TG_POLL_LIMIT N pesan terakhir tiap poll (default 3).
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  → notif via bot (andal). Kosong = Telethon 'me'.

jalan:
  python3 edel_watch.py                    # daemon (login first-run kalau belum)
  python3 edel_watch.py --catchup 20       # proses 20 pesan terakhir dulu, lalu listen
  python3 edel_watch.py --test "edel::...\nVZR57MP7P8"   # tes regex extract (offline)
  python3 edel_watch.py --code edel::xxxx  # register 1 kode manual (tanpa telegram)
"""
import os, sys, re, json, random, asyncio, functools, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BASEDIR = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv(path):
    """Muat KEY=VALUE dari .env ke os.environ (tanpa override yang sudah di-set). Tanpa dependency.
    Strip komentar inline ' # …' pada value TAK-berkutip (biar `KEY=val  # ket` → 'val')."""
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] not in ("'", '"'):          # value tak dikutip → potong komentar inline
                if v.startswith("#"):
                    v = ""                        # value cuma komentar (KEY=  # ket) → kosong
                else:
                    for sep in (" #", "\t#"):
                        if sep in v: v = v.split(sep, 1)[0].rstrip()
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
_load_dotenv(os.environ.get("EDEL_ENV") or os.path.join(BASEDIR, ".env"))

import requests
import register_http as RH  # register_one / register_batch / _append_account / gen_local / ...

try:
    from telethon import TelegramClient, events
except ImportError:
    print("Telethon belum ada. Install: pip3 install telethon"); sys.exit(1)

API_ID     = os.environ.get("EDEL_TG_API_ID", "").strip()
API_HASH   = os.environ.get("EDEL_TG_API_HASH", "").strip()
_WATCH_RAW = os.environ.get("EDEL_TG_WATCH", "handlpay,oneswap_community,edeldotfinance").strip()
WATCHES    = [w for w in re.split(r"[\s,]+", _WATCH_RAW) if w] or ["handlpay"]  # multi-channel
NOTIFY     = os.environ.get("EDEL_TG_NOTIFY", "me").strip() or "me"
USES       = int(os.environ.get("EDEL_CODE_USES", "1") or "1")   # akun per kode
PROBE      = os.environ.get("EDEL_PROBE", "").lower() in ("1", "true", "yes", "on")
WORKERS    = int(os.environ.get("REG_WORKERS", "4") or "4")      # floor paralel (dipakai kalau > jumlah kode)
MAXPAR     = int(os.environ.get("EDEL_WATCH_MAXPAR", "24") or "24")  # cap thread paralel (anti spawn ratusan)
PREWARM    = int(os.environ.get("EDEL_PREWARM", "16") or "16")   # identitas pre-gen sebelum kode drop
CATCHUP    = int(os.environ.get("EDEL_TG_CATCHUP", "0") or "0")
POLL       = float(os.environ.get("EDEL_TG_POLL", "1.0") or "1.0")
POLL_LIMIT = int(os.environ.get("EDEL_TG_POLL_LIMIT", "3") or "3")
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_CHAT   = (os.environ.get("TELEGRAM_CHAT_ID", "").strip()
              or (NOTIFY if NOTIFY.lstrip("-").isdigit() else ""))

SESSION   = os.path.join(BASEDIR, "watch.session")
SEEN_FILE = os.path.join(BASEDIR, "watch_seen.json")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
RE_PREFIXED = re.compile(r"[a-zA-Z0-9]+::" + _UUID)     # edel::uuid (kirim apa adanya)
RE_UUID     = re.compile(_UUID)                          # uuid telanjang
RE_SHORT    = re.compile(r"^[A-Z0-9]{8,20}$")            # kode pendek 1 baris (huruf+angka)
RE_GROUPED  = re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3,5}\b")  # E2WR-DLW4-PTZK-JJ3K-CCSD (4-6 grup)
_CUSTOM_RE  = os.environ.get("EDEL_CODE_RE", "").strip()
RE_CUSTOM   = re.compile(_CUSTOM_RE) if _CUSTOM_RE else None

_seen = set()   # dedup kode sudah diproses (persist watch_seen.json)
_pool = []      # identitas siap-pakai [(email, display)] — di-gen SEBELUM kode drop
_pool_lock = threading.Lock()


def _prewarm_pool(n):
    """Isi pool identitas unik sampai n (generate SEBELUM kode drop → klaim instan)."""
    with RH._flock():
        existing = {a["email"].split("@")[0]
                    for a in (json.load(open(RH.STATE)) if os.path.exists(RH.STATE) else [])}
    with _pool_lock:
        existing |= {e.split("@")[0] for e, _ in _pool}
        while len(_pool) < n:
            local = RH.gen_local(existing); existing.add(local)
            _pool.append((f"{local}@weling.web.id",
                          random.choice(RH.NAMES) + str(random.randint(100, 999))))
        return len(_pool)


def _take_identity(existing):
    """Ambil 1 identitas dari pool (instan) atau generate baru (fallback). Cek tak tabrakan."""
    with _pool_lock:
        while _pool:
            email, display = _pool.pop()
            if email.split("@")[0] not in existing:
                existing.add(email.split("@")[0]); return email, display
    local = RH.gen_local(existing); existing.add(local)
    return f"{local}@weling.web.id", random.choice(RH.NAMES) + str(random.randint(100, 999))


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
    """Ambil semua access code dari teks (dedup, urut kemunculan).
    EDEL_CODE_RE (group 1) kalau di-set; else pola: grup XXXX-XXXX-… (format Edel resmi),
    prefix::uuid, uuid telanjang, kode pendek [A-Z0-9]{8,20} 1 baris utuh."""
    if not text:
        return []
    codes, seen = [], set()
    def add(c):
        if c and c not in seen:
            seen.add(c); codes.append(c)
    if RE_CUSTOM:
        for m in RE_CUSTOM.finditer(text):
            add((m.group(1) if m.groups() else m.group(0)).strip())
        return codes
    # 0) format resmi Edel: 4-6 grup [A-Z0-9]{4} dipisah '-' (mis. E2WR-DLW4-PTZK-JJ3K-CCSD)
    for m in RE_GROUPED.finditer(text):
        add(m.group(0))
    covered = set()   # uuid yang sudah ketangkap versi prefix → jangan dobel telanjang
    for m in RE_PREFIXED.finditer(text):
        add(m.group(0)); covered.add(m.group(0).split("::", 1)[1])
    for m in RE_UUID.finditer(text):
        if m.group(0) not in covered:
            add(m.group(0))
    # kode pendek: kumpulkan baris yang UTUH cuma token [A-Z0-9]{8,20}. Kalau >=2 → itu
    # daftar kode, ambil semua (incl. yg tanpa digit spt ORMPJBKYZI). Kalau cuma 1 →
    # wajib ada huruf+angka (anti false-positive kata tunggal spt 'ANNOUNCEMENT').
    shorts = [line.strip().strip("`\"'") for line in text.splitlines()]
    shorts = [s for s in shorts if RE_SHORT.match(s)]
    if len(shorts) >= 2:
        for s in shorts: add(s)
    elif len(shorts) == 1:
        s = shorts[0]
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            add(s)
    return codes


def probe_code(code):
    """Cek kode valid TANPA finish (tak bikin akun). Return (True/False/None, why).
    None = tak yakin (error jaringan). Header browser wajib (edge WAF 404 utk UA non-browser)."""
    email = f"probe_{RH.secrets.token_hex(5)}@weling.web.id"
    try:
        r = requests.post(RH.BASE + "/auth/register/start",
            json={"email": email, "displayName": "probe", "accessCode": code},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "Mozilla/5.0", "Origin": RH.ORIGIN,
                     "Referer": RH.ORIGIN + "/register"}, timeout=25)
    except Exception as e:
        return None, f"probe err: {e}"
    if r.status_code == 200:
        return True, "valid"
    try:
        why = r.json().get("error", {}).get("code", "") or f"HTTP {r.status_code}"
    except Exception:
        why = f"HTTP {r.status_code}"
    return False, why


def _register_job(email, display, code):
    """Register 1 akun (identitas sudah dipesan). `edel::uuid` ditolak → coba lagi uuid
    telanjang. Return (ok, email, why).

    proxy=None SELALU: redeem FCFS → koneksi langsung paling cepat (proxy nambah hop/latency).
    tempik_inbox DITUNDA sampai MENANG: register/start konsumsi kode & ini balapan — jangan
    buang 2 HTTP call bikin inbox di jalur kritis; inbox cukup dibikin buat pemenang.
    Simpan akun HANYA kalau data passkey LENGKAP (privateKey wajib — tanpa itu akun tak bisa
    login/refresh selamanya). Kalau privateKey hilang, akun tetap didump ke accounts_incomplete.json
    agar tak lenyap, tapi ditandai gagal biar kelihatan."""
    tries = [code] + ([code.split("::", 1)[1]] if "::" in code else [])
    why = "?"
    for c in tries:
        res = RH.register_one(email, display, None, access_code=c)  # proxy=None → FCFS langsung
        if res.get("ok"):
            cred = res.get("credential") or {}
            if not cred.get("privateKey"):
                # akun ke-create di server tapi keypair hilang → JANGAN buang; dump mentah.
                try:
                    with open(os.path.join(BASEDIR, "accounts_incomplete.json"), "a") as f:
                        f.write(json.dumps(res) + "\n")
                except OSError: pass
                return False, email, "OK_BUT_NO_PRIVATEKEY(cek accounts_incomplete.json)"
            RH._append_account(res)       # atomik (_flock) → accounts.json lengkap: privateKey+publicKey+credId
            RH.tempik_inbox(email.split("@")[0])  # menang → baru bikin inbox (buat terima mail nanti)
            return True, email, "OK"
        why = res.get("why", "?")
        if "INVALID" not in why.upper():   # bukan soal kode → tak usah coba varian lain
            break
    return False, email, why


def _register_codes_blocking(codes):
    """Register PARALEL: tiap kode → USES akun (default 1). Return summary dict.
    Identitas (email unik) DI-GENERATE DULU serial (di bawah _flock) sebelum spawn thread —
    hindari race localpart dobel antar thread."""
    with RH._flock():
        existing = {a["email"].split("@")[0]
                    for a in (json.load(open(RH.STATE)) if os.path.exists(RH.STATE) else [])}
    jobs = []   # (email, display, code) — ambil identitas dari pool (instan) kalau ada
    for code in codes:
        for _ in range(max(1, USES)):
            email, display = _take_identity(existing)
            jobs.append((email, display, code))
    # paralel = jumlah job (1 thread/kode), di-cap MAXPAR. Floor REG_WORKERS kalau job sedikit.
    par = max(1, min(len(jobs), MAXPAR), min(WORKERS, len(jobs)))
    results, ok = [], 0
    with ThreadPoolExecutor(max_workers=par) as ex:
        futs = [ex.submit(_register_job, e, d, c) for e, d, c in jobs]
        for f in as_completed(futs):
            good, email, why = f.result()
            results.append((email, "OK" if good else why))
            if good: ok += 1
    return {"ok": ok, "n": len(jobs), "results": results, "total": RH._count_accts()}


async def process(client, codes, src="", force=False):
    """Proses kode baru: dedup ATOMIK → FIRE register secepatnya → notif/disk/refill belakangan.
    HOT PATH: jangan await notif/disk/probe sebelum tembak register (buang ratusan ms)."""
    # dedup atomik (asyncio single-thread; tak ada await antara cek & mark) — push & poll bisa
    # lihat kode sama nyaris barengan; yg pertama klaim, kedua dapat fresh kosong.
    fresh = list(codes) if force else [c for c in codes if c not in _seen]
    if not fresh:
        return
    for c in fresh:
        _seen.add(c)
    loop = asyncio.get_event_loop()
    if PROBE:   # opt-in (LAMBAT + bisa konsumsi kode single-use) — off by default
        checked = []
        for c in fresh:
            ok, why = probe_code(c)
            if ok is False:
                await notify(client, f"⛔ Kode <code>{c}</code> ditolak: {why} — skip.")
            else:
                checked.append(c)
        fresh = checked
        if not fresh:
            _save_seen(); return
    # FIRE register DULUAN — sebelum notif/disk (klaim = aksi tercepat, menang balapan)
    fut = loop.run_in_executor(None, functools.partial(_register_codes_blocking, fresh))
    asyncio.create_task(notify(client, f"🔔 {len(fresh)} kode baru dari <b>{src}</b> → register "
                                       f"({USES} akun/kode)…\n" + "\n".join(fresh[:20])))
    _save_seen()
    loop.run_in_executor(None, _prewarm_pool, PREWARM)   # isi ulang pool buat drop berikut
    try:
        res = await fut
    except Exception as e:
        await notify(client, f"❌ error register: {e}")
        return
    lines = [f"{'✅' if st == 'OK' else '❌'} {e}: {st}" for e, st in res["results"]]
    await notify(client, f"🏁 Selesai: {res['ok']}/{res['n']} akun jadi. "
                         f"Total akun: {res['total']}.\n" + ("\n".join(lines[:25]) or "(tak ada)"))


async def poller(client, chans):
    """Poll getHistory tiap POLL detik untuk SEMUA channel — deteksi kode tanpa nunggu push
    MTProto (bisa telat). chans: list (entity, title)."""
    if POLL <= 0:
        return
    from telethon.errors import FloodWaitError
    print(f"poller aktif: getHistory tiap {POLL}s (limit {POLL_LIMIT}) × {len(chans)} channel.")
    while True:
        for ent, title in chans:
            try:
                msgs = await client.get_messages(ent, limit=POLL_LIMIT)
                codes = []
                for m in reversed(msgs or []):
                    codes += extract_codes(m.message or "")
                if codes:
                    await process(client, codes, src=title + " (poll)")
            except FloodWaitError as e:
                print(f"poll FloodWait {e.seconds}s @ {title} — mundur"); await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                print(f"poll err @ {title}:", e)
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


async def catchup(client, chans):
    if CATCHUP <= 0:
        return
    for ent, title in chans:
        print(f"catchup: scan {CATCHUP} pesan terakhir @ {title}…")
        all_codes = []
        async for msg in client.iter_messages(ent, limit=CATCHUP):
            all_codes += extract_codes(msg.message or "")
        all_codes = list(dict.fromkeys(all_codes))
        new = [c for c in all_codes if c not in _seen]
        print(f"catchup {title}: {len(all_codes)} kode ({len(new)} baru).")
        if new:
            await process(client, new, src=f"{title} (catchup)")


async def amain():
    if not API_ID or not API_HASH:
        print("Set EDEL_TG_API_ID & EDEL_TG_API_HASH di .env (https://my.telegram.org)."); sys.exit(1)
    _load_seen()
    client = TelegramClient(SESSION, int(API_ID), API_HASH)
    await client.start()  # first run: nomor HP + OTP telegram (interaktif)
    me = await client.get_me()
    from telethon import utils
    chans, title_by_id = [], {}
    for w in WATCHES:
        try:
            ent = await client.get_entity(w)
            title = getattr(ent, "title", None) or getattr(ent, "username", w)
            chans.append((ent, title)); title_by_id[utils.get_peer_id(ent)] = title
        except Exception as e:
            print(f"⚠️ skip channel '{w}': {e}")
    if not chans:
        print("Tak ada channel valid (cek EDEL_TG_WATCH & keanggotaan akun)."); return
    names = ", ".join(t for _, t in chans)
    print(f"login sbg {me.first_name} (id {me.id}). Pantau {len(chans)} channel: {names}. "
          f"{USES} akun/kode. Notif → {'bot' if (BOT_TOKEN and BOT_CHAT) else NOTIFY}")
    # optimasi FCFS: warm koneksi TLS + pre-gen identitas SEBELUM kode drop
    RH.warm_connections(min(PREWARM, 12), log=print)
    npool = _prewarm_pool(PREWARM)
    print(f"prewarm {npool} identitas siap-pakai + koneksi TLS warm (poll {POLL}s).")
    await notify(client, f"👀 Edel watcher aktif. Pantau <b>{names}</b>. "
                         f"Access code auto-register ({USES} akun/kode, {npool} prewarm, poll {POLL}s).")

    @client.on(events.NewMessage(chats=[e for e, _ in chans]))
    async def _(event):
        codes = extract_codes(event.message.message or "")
        if codes:
            await process(client, codes, src=title_by_id.get(event.chat_id, "?"))

    await catchup(client, chans)
    print("listening (push handler + poller)… (Ctrl-C keluar)")
    ptask = asyncio.create_task(poller(client, chans))
    try:
        await client.run_until_disconnected()
    finally:
        ptask.cancel()


def _cli_test(text):
    codes = extract_codes(text.replace("\\n", "\n"))
    print("extracted:", codes or "(tak ada — set EDEL_CODE_RE atau cek format)")


def _cli_code(code):
    """Register 1 kode manual (tanpa telegram)."""
    if PROBE:
        ok, why = probe_code(code)
        print(f"probe: ok={ok} why={why}")
        if ok is False:
            print("kode ditolak, batal."); return
    res = _register_codes_blocking([code])
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
