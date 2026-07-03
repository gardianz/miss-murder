#!/usr/bin/env python3
"""
Listing Calls automation — login 1x (passkey/WebAuthn) -> simpan cookie edel_session -> semua operasi via HTTP.

Cara kerja:
  - Session cookie edel_session (~12 jam) disimpan per akun di accounts.json (field 'session').
  - Selama cookie valid, SEMUA request (portfolio, demand-index, listing-round, submit) pakai HTTP biasa
    (requests + proxy + cookie) TANPA browser. Login WebAuthn hanya saat cookie habis / 401.
  - Strategi pilih calls: tiap head-to-head, pilih asset dengan rank Demand Index lebih tinggi (rank kecil).

usage:
  python3 listing_bot.py --run [email]      # jalankan listing calls (semua akun ok, atau 1 email)
  python3 listing_bot.py --session [email]  # login & refresh cookie saja
  python3 listing_bot.py --status [email]   # cek portfolio + status round
"""
import os, sys, json, time, random, subprocess, requests, base64, hashlib, fcntl
from contextlib import contextmanager
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BASEDIR = os.path.dirname(os.path.abspath(__file__))  # portabel: relatif ke lokasi script (bukan hardcode)
ENGINE = os.path.join(BASEDIR, "engine")
STATE  = os.environ.get("EDEL_STATE") or os.path.join(BASEDIR, "accounts.json")
LOCK   = STATE + ".lock"
BASE   = "https://runway.edel.finance"
ORIGIN = "https://runway.edel.finance"

def _b64url(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def _std_dec(s): return base64.b64decode(s + "=" * (-len(s) % 4))

@contextmanager
def _flock():
    """Exclusive inter-process/thread lock via lockfile (fcntl)."""
    f = open(LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()

def load_accts(): return json.load(open(STATE)) if os.path.exists(STATE) else []

def save_accts(a):
    """Atomic write (tmp + rename) di bawah lock."""
    with _flock():
        tmp = STATE + ".tmp"
        json.dump(a, open(tmp, "w"), indent=2)
        os.replace(tmp, STATE)

def update_account(email, patch):
    """Read-modify-write 1 akun secara ATOMIK (aman untuk paralel). patch(acct_dict) di-mutate in place."""
    with _flock():
        accts = json.load(open(STATE))
        hit = None
        for a in accts:
            if a["email"] == email:
                patch(a); hit = a; break
        tmp = STATE + ".tmp"
        json.dump(accts, open(tmp, "w"), indent=2)
        os.replace(tmp, STATE)
        return hit
def load_proxies():
    fp = os.path.expanduser("~/.proxies.txt")
    return [l.strip() for l in open(fp)] if os.path.exists(fp) else []
PROXIES = [l for l in load_proxies() if l and not l.startswith("#")]

_ACCTS = None  # di-set di main(), dipakai api() untuk auto re-login

NO_PROXY = os.environ.get("NO_PROXY", "").lower() in ("1", "true", "yes", "on")  # paksa tanpa proxy

def _proxy_dict(proxy):
    if NO_PROXY: return None  # abaikan proxy akun/pool -> koneksi langsung
    return {"http": proxy, "https": proxy} if proxy else None

# timeout (connect, read): connect pendek supaya proxy mati cepat gagal & dirotasi
HTTP_TIMEOUT = (float(os.environ.get("HTTP_CONNECT_TO", "4")), float(os.environ.get("HTTP_READ_TO", "11")))
API_DEADLINE = float(os.environ.get("API_DEADLINE", "40"))  # budget total per request (detik) — retry proxy sampai budget habis

def _raw_http(cookie, proxy, method, path, body, timeout=HTTP_TIMEOUT):
    """Satu request mentah. Return (status:int|'ERR', data, set_cookie_obj_or_None)."""
    s = requests.Session()
    if cookie: s.cookies.set("edel_session", cookie, domain="runway.edel.finance")
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0", "Origin": ORIGIN, "Referer": ORIGIN + "/desk"})
    try:
        r = s.request(method, BASE + path, json=body, proxies=_proxy_dict(proxy), timeout=timeout)
        try: data = r.json()
        except Exception: data = r.text
        return r.status_code, data, None
    except Exception as e:
        return "ERR", str(e)[:100], None

def api(acct, method, path, body=None, retries=5, auth=True):
    """Request KEBAL: auto re-login saat 401/403, backoff SINGKAT saat 5xx, rotasi proxy CEPAT saat
    network error (tanpa backoff lama). Refresh sesi proaktif kalau cookie expired. Return (status, data)."""
    accts = _ACCTS if _ACCTS is not None else [acct]
    if auth and not session_valid(acct):
        refresh_session(acct, accts)
    proxy = acct.get("proxy")
    alt_proxies = [p for p in PROXIES if p != proxy]
    relogin_count = 0
    last = None
    t0 = time.time()
    attempt = 0
    # RETRY sampai budget habis (bukan cap 5x) — proxy mati/5xx dirotasi terus sampai API_DEADLINE.
    # Sebelumnya retries=5 bikin ERR muncul dalam ~1.5s saat proxy connection-refused.
    while time.time() - t0 <= API_DEADLINE:
        attempt += 1
        cookie = (acct.get("session") or {}).get("value") if auth else None
        st, data, _ = _raw_http(cookie, proxy, method, path, body)
        if st == "ERR":
            last = data
            # proxy/jaringan gagal -> langsung rotasi proxy & coba lagi (jeda kecil saja)
            if alt_proxies: proxy = random.choice(alt_proxies)
            time.sleep(0.3)
            continue
        if st in (401, 403) and auth:
            if relogin_count < 3:
                relogin_count += 1
                if refresh_session(acct, accts): continue
            last = f"{st} (auth; relogin x{relogin_count})"
            time.sleep(1)
            continue
        if st >= 500:
            last = f"{st}"
            time.sleep(min(1.6 ** min(attempt, 6), 6))  # backoff singkat
            continue
        return st, data  # 2xx/4xx (selain 401/403) -> hasil final
    return "ERR", last

def http(acct, method, path, body=None):  # kompat lama
    return api(acct, method, path, body)

def session_valid(acct):
    sess = acct.get("session") or {}
    return bool(sess.get("value")) and sess.get("expires", 0) > time.time() + 300

def refresh_session(acct, accts, tries=6):
    """Login via HTTP MURNI (Ed25519 sign, tanpa browser) -> simpan cookie edel_session.
    Jauh lebih andal daripada puppeteer. WebAuthn assertion dibuat manual dari private key."""
    cred = acct.get("credential") or {}
    if not cred.get("privateKey"): return False
    email = acct["email"]; proxy = acct.get("proxy")
    proxies = None if NO_PROXY else ({"http": proxy, "https": proxy} if proxy else None)
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0", "Origin": ORIGIN, "Referer": ORIGIN + "/login"})
    def post(path, body):
        for _ in range(tries):
            try:
                r = s.post(BASE + path, json=body, proxies=proxies, timeout=25)
                if r.status_code < 500: return r
            except Exception: pass
            time.sleep(3)
        return None
    try:
        r = post("/auth/login/start", {"email": email})
        if not r or r.status_code != 200:
            print(f"  [{email}] login/start gagal: {r.status_code if r else 'ERR'}"); return False
        opts = r.json()["options"]
        challenge = opts["challenge"]; rp_id = opts["rpId"]
        cred_id = opts["allowCredentials"][0]["id"]
        client_data = json.dumps({"type": "webauthn.get", "challenge": challenge,
            "origin": ORIGIN, "crossOrigin": False}, separators=(",", ":")).encode()
        rp_hash = hashlib.sha256(rp_id.encode()).digest()
        auth_data = rp_hash + bytes([0x05]) + int(time.time()).to_bytes(4, "big")  # flags UP+UV, counter=unix
        signed = auth_data + hashlib.sha256(client_data).digest()
        key = serialization.load_der_private_key(_std_dec(cred["privateKey"]), password=None)
        sig = key.sign(signed)
        uh = _std_dec(cred["userHandle"])
        body = {"response": {"id": cred_id, "rawId": cred_id, "response": {
            "authenticatorData": _b64url(auth_data), "clientDataJSON": _b64url(client_data),
            "signature": _b64url(sig), "userHandle": _b64url(uh)},
            "type": "public-key", "clientExtensionResults": {}, "authenticatorAttachment": "platform"}}
        r = post("/auth/login/finish", body)
        if not r or r.status_code != 200:
            print(f"  [{email}] login/finish gagal: {r.status_code if r else 'ERR'} {r.text[:80] if r else ''}"); return False
        # ambil cookie + expiry PRESISI dari cookiejar (requests parse Max-Age/Expires dari Set-Cookie)
        cobj = next((c for c in s.cookies if c.name == "edel_session"), None)
        if not cobj:
            print(f"  [{email}] no edel_session cookie"); return False
        expires = cobj.expires if cobj.expires else (time.time() + 12 * 3600)
        acct["session"] = {"value": cobj.value, "expires": expires}  # in-memory
        update_account(email, lambda a: a.__setitem__("session", acct["session"]))  # atomic ke file
        return True
    except Exception as e:
        print(f"  [{email}] refresh error: {str(e)[:100]}"); return False

def ensure_session(acct, accts):
    if session_valid(acct): return True
    return refresh_session(acct, accts)

def demand_rank(acct):
    """Map assetId -> rank (rank kecil = demand tinggi). Return {} kalau gagal."""
    st, data = http(acct, "GET", "/demand-index")
    if st == 200 and isinstance(data, dict):
        return {x["assetId"]: x["rank"] for x in data.get("rankings", [])}
    return {}

def pick_asset(opt, rankmap):
    """Pilih assetA/assetB dengan rank demand lebih tinggi (angka lebih kecil). Default assetA."""
    a, b = opt["assetAId"], opt["assetBId"]
    ra = rankmap.get(a, 10**9); rb = rankmap.get(b, 10**9)
    return a if ra <= rb else b

def units(amt): return int(amt["units"]) / (10 ** amt["decimals"]) if amt else 0

MIN_EDELX = float(os.environ.get("MIN_EDELX", "0"))  # skip kalau available < ini (0 = asal >0)

def _err_code(data):
    if isinstance(data, dict): return (data.get("error") or {}).get("code")
    return None

def _iso_lt(a, b):
    """a < b untuk timestamp ISO (string compare aman krn ISO Z sama format)."""
    return bool(a) and bool(b) and a < b

# fase listing call gaya web (diturunkan dari status+timing+posisi; server tak kirim string ini)
PHASE_LABEL = {
    "idle": "Menunggu window", "preparing": "Preparing listing calls",
    "allocation_pending": "Allocation pending", "submitted": "Selection submitted",
    "demand_pending": "Demand index pending", "final": "Demand index final", "unknown": "?",
}

def phase_from(lr, edelx):
    """Turunkan fase dari /listing-round (lr) + balance EDELx (dict units). Return code (lihat PHASE_LABEL)."""
    if not isinstance(lr, dict): return "unknown"
    rnd = lr.get("round") or {}
    prev = lr.get("preview")
    snow = lr.get("serverTime")
    closes = (rnd.get("timing") or (lr.get("currentWindow", {}) or {}).get("timing") or {}).get("selectionClosesAt")
    staked = (units(edelx.get("staked")) + units(edelx.get("locked"))) if edelx else 0  # locked = masih dlm settlement
    status = rnd.get("status")
    lock = rnd.get("stakeLockStatus")
    if status == "SUBMITTED":
        if lock and lock != "locked":  # lock_pending: submit ok tapi stake belum ter-lock (async)
            return "allocation_pending"
        if staked <= 0:               # sudah pernah locked lalu lepas → settlement selesai
            return "final"
        if _iso_lt(snow, closes):      # masih dalam window pemilihan
            return "submitted"
        return "demand_pending"        # window tutup, nunggu demand index/settlement
    if prev:                           # ada preview, belum submit
        return "preparing"
    if staked > 0:                     # ke-lock tapi tak ada round aktif (edge)
        return "demand_pending"
    return "idle"

def _bump_stats(a, window, stake):
    """Catat 1 submit sukses ke acct['stats'] (dedup per window)."""
    s = a.setdefault("stats", {"submitted": 0, "windows": []})
    wins = s.setdefault("windows", [])
    if window and window in wins: return  # sudah dihitung window ini
    if window: wins.append(window)
    if len(wins) > 200: del wins[:-200]  # cap memori
    s["submitted"] = s.get("submitted", 0) + 1
    s["last_submit"] = time.time()
    s["last_window"] = window
    s["last_stake"] = stake

def do_listing(acct, accts, rankmap, log=print):
    """Return status: submitted | locked (menunggu settlement) | no_edelx | below_min |
    already (sudah submit window ini) | no_session | freeze | pending | fail."""
    em = acct["email"]
    # 1) sesi/login — kalau akun TAK ADA di server atau kredensial invalid, login gagal -> skip aman
    if not ensure_session(acct, accts):
        if is_manual(acct):  # akun browser: cookie mati & tak ada key buat relogin
            log(f"[{em}] SKIP: cookie akun manual kedaluwarsa — ekspor ulang dari Chrome extension"); return "cookie_expired"
        log(f"[{em}] SKIP: login gagal (akun tak terdaftar / server down)"); return "no_session"
    # 2) saldo EDELx
    st, pf = api(acct, "GET", "/portfolio")  # api auto-handle 401/5xx/proxy
    if not isinstance(pf, dict):
        log(f"[{em}] SKIP: portfolio tak terbaca ({st})"); return "fail"
    edelx = next((b for b in pf.get("balances", []) if b.get("instrumentId") == "EDELx"), None)
    avail = units(edelx.get("available")) if edelx else 0
    locked_amt = units(edelx.get("locked")) if edelx else 0
    staked_amt = units(edelx.get("staked")) if edelx else 0
    held = avail + locked_amt + staked_amt  # total holding SEBENARNYA (server 'total' TIDAK termasuk locked!)
    if avail <= 0:
        if (locked_amt + staked_amt) > 0:  # ada EDELx tapi terkunci (staked/locked) -> MENUNGGU settlement lepas
            log(f"[{em}] SKIP: EDELx terkunci {locked_amt+staked_amt:.2f} (menunggu settlement)"); return "locked"
        log(f"[{em}] SKIP: EDELx=0 (belum deposit)"); return "no_edelx"
    if avail < MIN_EDELX:
        log(f"[{em}] SKIP: EDELx {avail:.4f} < minimum {MIN_EDELX}"); return "below_min"
    # 3) buka round
    st, r = api(acct, "POST", "/listing-round", {})
    if st not in (200, 201) or not isinstance(r, dict):
        code = _err_code(r)
        if code in ("stake_lock_failed", "listing_stake_holdings_not_found"):
            log(f"[{em}] SKIP: EDELx tak cukup untuk buka round ({code})"); return "no_edelx"
        elif code in ("daily_round_limit", "listing_round_limit"):
            log(f"[{em}] SKIP: sudah kena batas window ({code})"); return "already"
        elif code in ("round_selection_not_open", "listing_decision_cycle_exhausted"):
            log(f"[{em}] SKIP: window belum buka ({code})"); return "pending"
        elif code in ("vault_stake_locker_disabled", "vault_migration_freeze"):
            log(f"[{em}] SKIP: listing calls sedang freeze ({code})"); return "freeze"
        elif code == "previous_round_settlement_pending":
            log(f"[{em}] SKIP: settlement round sebelumnya belum selesai"); return "locked"
        log(f"[{em}] buka round gagal: {st} {code or ''}"); return "fail"
    preview = r.get("preview")
    if not preview:
        log(f"[{em}] SKIP: sudah submit window ini"); return "already"
    # 4) pilih (demand index) + submit
    picks = [{"listingDecisionId": o["listingDecisionId"], "assetId": pick_asset(o, rankmap)} for o in preview["options"]]
    st, sub = api(acct, "POST", "/listing-round/submit", {"previewId": preview["id"], "picks": picks})
    if st == 200 and isinstance(sub, dict) and sub.get("round", {}).get("status") == "SUBMITTED":
        stake = units(preview.get("stakeAmount")); wid = preview.get("roundWindowId")
        update_account(em, lambda a: _bump_stats(a, wid, stake))  # catat statistik (atomik, dedup window)
        log(f"[{em}] ✓ SUBMITTED {len(picks)} picks, stake={stake:.2f} EDELx (window {(wid or '')[11:16]})")
        return "submitted"
    log(f"[{em}] submit gagal: {st} {_err_code(sub) or ''}"); return "fail"

def is_manual(acct):
    """Akun login-browser: punya cookie session tapi TAK punya private key (passkey browser
    tak bisa di-ekspor). Bot pakai cookie sampai mati, TAK bisa auto-relogin."""
    return not (acct.get("credential") or {}).get("privateKey") and bool((acct.get("session") or {}).get("value"))

def targets(accts, email=None):
    # akun BOT (punya private key) ATAU akun MANUAL (punya cookie session) sama-sama ikut
    t = [a for a in accts if a.get("ok") and (
        (a.get("credential") or {}).get("privateKey") or (a.get("session") or {}).get("value"))]
    if email: t = [a for a in t if a["email"] == email]
    return t

def _window_info(accts):
    """Return (roundWindowId, selectionClosesAt_str, server_now_str) dari 1 akun bersesi."""
    for a in targets(accts):
        if session_valid(a) or ensure_session(a, accts):
            st, lr = api(a, "GET", "/listing-round")
            if isinstance(lr, dict):
                cw = lr.get("currentWindow", {}); t = cw.get("timing", {})
                return cw.get("roundWindowId"), t.get("selectionClosesAt"), lr.get("serverTime")
            break
    return None, None, None

# status auto (dibaca UI dashboard)
AUTO_STATE = {"running": False, "window": None, "server": None, "submitted": 0, "locked": 0,
              "no_edelx": 0, "already": 0, "fail": 0, "next_in": 0, "last": 0, "total_submitted": 0}

def auto_loop(accts, workers=8, poll_slow=None, poll_fast=None, log=print, stop=None):
    """AUTO: jalan terus, submit listing calls tiap window. Idempotent → akun yang EDELx-nya baru
    lepas dari settlement ter-submit di window BERJALAN pada poll berikutnya. POLL ADAPTIF (cepat saat
    ada 'locked', lambat saat idle). KEBAL (tiap iterasi try). stop=Event untuk berhenti bersih."""
    global _ACCTS
    poll_slow = poll_slow if poll_slow is not None else int(os.environ.get("AUTO_POLL", "180"))
    poll_fast = poll_fast if poll_fast is not None else int(os.environ.get("AUTO_POLL_FAST", "25"))
    hard_retry = int(os.environ.get("HARD_RETRY", "6"))  # retry LANGSUNG akun fail/no_session per siklus
    last_win = None
    AUTO_STATE["running"] = True
    log(f"[auto] mulai — poll adaptif {poll_fast}s/{poll_slow}s, hard-retry {hard_retry}x.")
    while not (stop and stop.is_set()):
        wait = poll_slow
        try:
            accts = load_accts(); _ACCTS = accts
            wid, closes, snow = _window_info(accts)
            AUTO_STATE["window"], AUTO_STATE["server"] = wid, snow
            if wid and wid != last_win:
                log(f"[auto] === WINDOW BARU {wid[11:16]} (server {(snow or '')[11:19]}) ===")
                last_win = wid
            res = run_all(accts, workers=workers, log=log, stop=stop)
            # RETRY LANGSUNG: akun yang GAGAL jaringan / sesi (bukan locked/no_edelx) — ini yang HARUS ikut.
            # Ulang terus dalam siklus ini sampai bersih atau batas hard_retry (jeda pendek antar percobaan).
            for rp in range(hard_retry):
                bad = [e for e, s in res.items() if s in ("fail", "no_session")]
                if not bad or (stop and stop.is_set()): break
                log(f"[auto] retry-langsung {len(bad)} akun gagal (percobaan {rp+1}/{hard_retry})")
                time.sleep(2)
                res.update(run_all(accts, emails=set(bad), workers=workers, log=log, stop=stop))
            c = {}
            for v in res.values(): c[v] = c.get(v, 0) + 1
            submitted, locked, fail = c.get("submitted", 0), c.get("locked", 0), c.get("fail", 0)
            AUTO_STATE["total_submitted"] += submitted
            AUTO_STATE.update(submitted=submitted, locked=locked, fail=fail,
                              no_edelx=c.get("no_edelx", 0), already=c.get("already", 0), last=time.time())
            # partisipasi: sudah ikut window ini = submitted + already ; belum = locked (nunggu settle) + fail sisa
            joined = submitted + c.get("already", 0)
            waiting = locked + c.get("pending", 0)
            if submitted: log(f"[auto] window {(wid or '')[11:16]}: +{submitted} submit (total {AUTO_STATE['total_submitted']})")
            ck = c.get("cookie_expired", 0)
            log(f"[auto] partisipasi window {(wid or '')[11:16]}: ikut={joined} nunggu-settle={waiting} gagal-sisa={fail} belum-deposit={c.get('no_edelx',0)}"
                + (f" cookie-mati={ck}" if ck else ""))
            # poll CEPAT selama masih ada yang bisa diulang (locked nunggu settle / fail / no_session / pending)
            retry = locked + fail + c.get("no_session", 0) + c.get("pending", 0)
            wait = poll_fast if retry > 0 else poll_slow
            if fail: log(f"[auto] {fail} akun MASIH gagal setelah hard-retry → ULANG {wait}s")
            if locked: log(f"[auto] {locked} akun menunggu settlement server → cek lagi {wait}s")
        except Exception as e:
            log(f"[auto] err: {str(e)[:90]}"); wait = poll_fast
        # tidur interruptible (Ctrl-C / stop langsung berhenti)
        for _ in range(int(wait * 2)):
            if stop and stop.is_set(): break
            AUTO_STATE["next_in"] = (int(wait * 2) - _) / 2
            time.sleep(0.5)
    AUTO_STATE["running"] = False
    log("[auto] berhenti.")

def run_all(accts, emails=None, workers=8, log=print, stop=None):
    """Jalankan listing calls PARALEL. Thread-safe (api + update_account atomik). stop=Event untuk
    batalkan futures yang belum jalan saat berhenti. Return {email: status}."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    global _ACCTS
    _ACCTS = accts
    ts = [a for a in targets(accts) if (emails is None or a["email"] in emails)]
    if not ts: return {}
    rankmap = {}
    for a in ts:
        if stop and stop.is_set(): return {}
        if ensure_session(a, accts):
            rankmap = demand_rank(a)
            if rankmap: break
    log(f"demand rank: {len(rankmap)} assets | akun: {len(ts)} | workers: {workers}")
    results = {}
    def worker(a):
        if stop and stop.is_set(): return a["email"], "stopped"
        try: return a["email"], do_listing(a, accts, rankmap, log=log)
        except Exception as e: log(f"[{a['email']}] error: {str(e)[:100]}"); return a["email"], "fail"
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(worker, a) for a in ts]
    try:
        for fut in as_completed(futs):
            em, okk = fut.result(); results[em] = okk
            if stop and stop.is_set():
                for f in futs: f.cancel()
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return results

def account_history(acct, accts):
    """Statistik submit lokal + saldo live + status round sekarang. Return dict."""
    s = acct.get("stats") or {}
    row = {"email": acct["email"], "submitted": s.get("submitted", 0),
           "last_window": s.get("last_window"), "last_stake": s.get("last_stake"),
           "staked": 0.0, "available": 0.0, "total": 0.0, "round": None, "phase": "idle"}
    if session_valid(acct) or ensure_session(acct, accts):
        st, pf = api(acct, "GET", "/portfolio")
        e = None
        if isinstance(pf, dict):
            e = next((b for b in pf.get("balances", []) if b.get("instrumentId") == "EDELx"), None)
            if e:
                row["staked"] = units(e.get("staked")) + units(e.get("locked"))  # terkunci total (staked+locked)
                row["available"] = units(e.get("available"))
                row["total"] = units(e.get("total"))
        st2, lr = api(acct, "GET", "/listing-round")
        rnd = (lr.get("round") if isinstance(lr, dict) else None)
        row["round"] = rnd.get("status") if rnd else None
        row["phase"] = phase_from(lr, e)
    return row

def show_history(accts, email=None, live=True):
    """Tabel: berapa listing call berhasil per akun + total keseluruhan. live=cek saldo server."""
    from concurrent.futures import ThreadPoolExecutor
    ts = targets(accts, email)
    if not ts: print("tak ada akun target"); return
    if live:
        with ThreadPoolExecutor(max_workers=min(10, len(ts))) as ex:
            rows = list(ex.map(lambda a: account_history(a, accts), ts))
    else:
        rows = [{"email": a["email"], "submitted": (a.get("stats") or {}).get("submitted", 0),
                 "last_window": (a.get("stats") or {}).get("last_window"),
                 "staked": 0, "available": 0, "round": None} for a in ts]
    rows.sort(key=lambda r: r["submitted"], reverse=True)
    print(f"\n{'EMAIL':<30}{'SUB':>4} {'LOCKED':>9}{'AVAIL':>9}  {'FASE':<24}")
    print("─" * 82)
    total_sub = 0; ph_count = {}
    for r in rows:
        total_sub += r["submitted"]
        fase = PHASE_LABEL.get(r.get("phase", "idle"), "?")
        ph_count[fase] = ph_count.get(fase, 0) + 1
        print(f"{r['email']:<30}{r['submitted']:>4} {r['staked']:>9.2f}{r['available']:>9.2f}  {fase:<24}")
    print("─" * 82)
    n_active = sum(1 for r in rows if r["submitted"] > 0)
    print(f"TOTAL listing call sukses: {total_sub}  |  akun aktif: {n_active}/{len(rows)}")
    if live:
        print("FASE:", " | ".join(f"{k}={v}" for k, v in sorted(ph_count.items(), key=lambda x: -x[1])))

def main():
    global _ACCTS
    mode = sys.argv[1] if len(sys.argv) > 1 else "--status"
    email = next((a for a in sys.argv[2:] if not a.startswith("-")), None)  # arg non-flag pertama
    accts = load_accts()
    _ACCTS = accts  # api() pakai ini untuk auto re-login + persist cookie baru
    ts = targets(accts, email)
    if not ts: print("tak ada akun target"); return

    if mode == "--session":
        for a in ts:
            print(f"[{a['email']}] {'valid' if session_valid(a) else 'refresh...'}")
            if not session_valid(a): refresh_session(a, accts)
        return

    if mode == "--status":
        for a in ts:
            if not ensure_session(a, accts): continue
            st, pf = http(a, "GET", "/portfolio")
            bal = {b['instrumentId']: {k: units(b[k]) for k in ('available','locked','staked','total')} for b in (pf.get('balances',[]) if isinstance(pf,dict) else [])}
            st2, lr = http(a, "GET", "/listing-round")
            rnd = (lr.get('round') if isinstance(lr,dict) else None)
            print(f"[{a['email']}] EDELx={bal.get('EDELx')} round={rnd.get('status') if rnd else None}")
        return

    if mode == "--history":
        show_history(accts, email, live="--fast" not in sys.argv)
        return

    if mode == "--run":
        workers = int(os.environ.get("WORKERS", "8"))
        emails = [email] if email else None
        res = run_all(accts, emails=emails, workers=workers)
        ok = sum(1 for v in res.values() if v == "submitted")
        print(f"\n[done] {ok}/{len(res)} akun submit")
        return

    if mode == "--auto":
        workers = int(os.environ.get("WORKERS", "8"))
        try: auto_loop(accts, workers=workers)
        except KeyboardInterrupt: print("\n[auto] berhenti")
        return

    print("mode: --run | --auto | --session | --status | --history  [email]")

if __name__ == "__main__":
    main()
