#!/usr/bin/env python3
"""
Register akun FULL HTTP (tanpa browser) — bikin WebAuthn attestation 'none' + keypair Ed25519 sendiri.
Server Edel: pubKeyCredParams termasuk alg -8 (Ed25519), attestation 'none' → bisa dibuat manual.

usage:
  python3 register_http.py [N]         # register N akun (default 1)
  python3 register_http.py --until M   # loop register sampai TOTAL akun >= M
  python3 register_http.py --selftest  # validasi builder CBOR vs data tertangkap
"""
import os, sys, json, time, uuid, hashlib, base64, secrets, random, fcntl
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BASE = "https://runway.edel.finance"; ORIGIN = BASE
BASEDIR = os.path.dirname(os.path.abspath(__file__))  # portabel: relatif ke lokasi script
STATE = os.environ.get("EDEL_STATE") or os.path.join(BASEDIR, "accounts.json")
TEMPIK = "https://tempik.weling.web.id/api"

def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def b64u_dec(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

# ── CBOR minimal (cukup untuk attestationObject) ───────────────────────
def _cbor_uint(major, n):
    if n < 24: return bytes([major | n])
    if n < 256: return bytes([major | 24, n])
    if n < 65536: return bytes([major | 25]) + n.to_bytes(2, "big")
    return bytes([major | 26]) + n.to_bytes(4, "big")
def _cbor_tstr(s): b = s.encode(); return _cbor_uint(0x60, len(b)) + b
def _cbor_bstr(b): return _cbor_uint(0x40, len(b)) + b

def cose_ed25519(pub_raw):
    # COSE_Key OKP Ed25519: {1:1, 3:-8, -1:6, -2:pub}
    return (bytes([0xa4]) + bytes([0x01, 0x01]) + bytes([0x03, 0x27]) +
            bytes([0x20, 0x06]) + bytes([0x21]) + _cbor_bstr(pub_raw))

def auth_data(rp_id, cred_id, pub_raw, sign_count=0, aaguid=b"\x00" * 16, flags=0x45):
    return (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) +
            sign_count.to_bytes(4, "big") + aaguid +
            len(cred_id).to_bytes(2, "big") + cred_id + cose_ed25519(pub_raw))

def attestation_object(ad):
    # CBOR map3: {"fmt":"none","attStmt":{},"authData":ad}
    return (bytes([0xa3]) + _cbor_tstr("fmt") + _cbor_tstr("none") +
            _cbor_tstr("attStmt") + bytes([0xa0]) +
            _cbor_tstr("authData") + _cbor_bstr(ad))

def selftest():
    # data tertangkap dari browser
    cap_att = "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YViBNQZXzLBg59qAOKDJWRYLdXauGs7gK_MaMaW-JypMyR1FAAAAAQECAwQFBgcIAQIDBAUGBwgAIHUwj_WT6lXpjhcDpe88hTIJor4_0c-fx0sZofQwqpgXpAEBAycgBiFYIE8Y-mQyMuwbvSA7qR7RLx5swF8pejIoyBHO6cNATAyU"
    cap_pub_spki = "MCowBQYDK2VwAyEATxj6ZDIy7Bu9IDupHtEvHmzAXyl6MijIEc7pw0BMDJQ"
    cred_id = b64u_dec("dTCP9ZPqVemOFwOl7zyFMgmivj_Rz5_HSxmh9DCqmBc")
    pub_raw = b64u_dec(cap_pub_spki)[-32:]  # SPKI Ed25519 = 12B header + 32B key
    aaguid = bytes([1,2,3,4,5,6,7,8,1,2,3,4,5,6,7,8])
    ad = auth_data("edel.finance", cred_id, pub_raw, sign_count=1, aaguid=aaguid, flags=0x45)
    att = attestation_object(ad)
    mine = b64u(att)
    print("match attestationObject:", mine == cap_att)
    if mine != cap_att:
        print(" mine:", mine[:80]); print(" cap :", cap_att[:80])

# ── register HTTP ──────────────────────────────────────────────────────
NAMES = ["Alice","Nova","Kai","Zara","Leo","Mira","Rex","Iris","Dax","Luna","Vic","Ash","Neo","Remy","Skye"]
W1 = ["harimau","rambutan","dewi","kuda","awan","pinus","kenari","cakra","putri","bulan","abimanyu","cerita","gunung","galaksi","salak","dimas","gemericik","merak","elang","melati","surya","bayu","teratai","rusa","kabut","ombak","embun","rajawali","kunang","seruni"]
W2 = ["bukit","kelabu","padang","nila","biru","kuning","merah","muda","ikhlas","hijau","hangat","kencang","karya","damai","lembah","jingga","perak","emas","pagi","malam","rimba","sunyi","tenang","cerah","indah","permai","abadi","lestari","mekar","ceria"]

def load_proxies():
    fp = os.path.expanduser("~/.proxies.txt")
    return [l.strip() for l in open(fp) if l.strip() and not l.startswith("#")] if os.path.exists(fp) else []

def gen_local(existing):
    for _ in range(300):
        w = random.choice(W1) + random.choice(W2)
        if random.random() < 0.6: w += str(random.randint(10, 99))
        if w not in existing: return w
    return random.choice(W1) + random.choice(W2) + str(random.randint(1000, 9999))

def tempik_inbox(local):
    try:
        s = requests.get(f"{TEMPIK}/session", timeout=15).json()["sessionId"]
        requests.post(f"{TEMPIK}/inboxes", headers={"x-session-id": s, "Content-Type": "application/json"},
            json={"localPart": local}, timeout=15)
    except Exception: pass

NO_PROXY = os.environ.get("NO_PROXY", "").lower() in ("1", "true", "yes", "on")

def register_one(email, display, proxy, tries=6):
    proxies = None if NO_PROXY else ({"http": proxy, "https": proxy} if proxy else None)
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0", "Origin": ORIGIN, "Referer": ORIGIN + "/register"})
    def post(path, body):
        for _ in range(tries):
            try:
                r = s.post(BASE + path, json=body, proxies=proxies, timeout=25)
                if r.status_code < 500: return r
            except Exception: pass
            time.sleep(2)
        return None
    # 1) start
    r = post("/auth/register/start", {"email": email, "displayName": display})
    if not r or r.status_code != 200: return {"ok": False, "email": email, "why": f"start {r.status_code if r else 'ERR'}"}
    opts = r.json()["options"]
    challenge = opts["challenge"]; rp_id = opts["rp"]["id"]
    # 2) keypair Ed25519 + credential
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    priv_pkcs8 = priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub_spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    cred_id = secrets.token_bytes(32)
    ad = auth_data(rp_id, cred_id, pub_raw, sign_count=0)
    att = attestation_object(ad)
    client_data = json.dumps({"type": "webauthn.create", "challenge": challenge,
        "origin": ORIGIN, "crossOrigin": False}, separators=(",", ":")).encode()
    body = {"response": {"id": b64u(cred_id), "rawId": b64u(cred_id), "response": {
        "attestationObject": b64u(att), "clientDataJSON": b64u(client_data),
        "transports": ["internal"], "publicKeyAlgorithm": -8,
        "publicKey": b64u(pub_spki), "authenticatorData": b64u(ad)},
        "type": "public-key", "clientExtensionResults": {"credProps": {"rk": True}},
        "authenticatorAttachment": "platform"}}
    # 3) finish
    r = post("/auth/register/finish", body)
    if not r or r.status_code not in (200, 201):
        return {"ok": False, "email": email, "why": f"finish {r.status_code if r else 'ERR'} {r.text[:100] if r else ''}"}
    prof = r.json().get("profile", {})
    return {
        "email": email, "displayName": display, "ok": True,
        "profileId": prof.get("id"), "hostedPartyId": prof.get("hostedPartyId"), "proxy": proxy,
        "finalUrl": BASE + "/desk", "credVerified": True,  # kita yang buat keypair → pasti cocok
        "credential": {
            "credentialId": base64.b64encode(cred_id).decode(),
            "isResidentCredential": True,
            "privateKey": base64.b64encode(priv_pkcs8).decode(),
            "publicKey": base64.b64encode(pub_spki).decode(),
            "userHandle": base64.b64encode(email.encode()).decode(),
            "rpId": rp_id, "signCount": 1,
        },
    }

@contextmanager
def _flock():
    f = open(STATE + ".lock", "w")
    try: fcntl.flock(f, fcntl.LOCK_EX); yield
    finally: fcntl.flock(f, fcntl.LOCK_UN); f.close()

def _append_account(acct):
    """Append 1 akun ke accounts.json secara ATOMIK (aman paralel)."""
    with _flock():
        accts = json.load(open(STATE)) if os.path.exists(STATE) else []
        accts.append(acct)
        tmp = STATE + ".tmp"; json.dump(accts, open(tmp, "w"), indent=2); os.replace(tmp, STATE)

def register_batch(n, workers=4, log=print):
    """Register n akun PARALEL full-HTTP. Return jumlah sukses."""
    proxies = load_proxies()
    with _flock():
        existing = {a["email"].split("@")[0] for a in (json.load(open(STATE)) if os.path.exists(STATE) else [])}
    # siapkan n identitas unik dulu
    jobs = []
    for _ in range(n):
        local = gen_local(existing); existing.add(local)
        jobs.append((f"{local}@weling.web.id", random.choice(NAMES) + str(random.randint(100, 999)),
                     local, random.choice(proxies) if proxies else None))
    ok = 0
    def one(job):
        email, display, local, proxy = job
        tempik_inbox(local)
        return register_one(email, display, proxy)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(one, j) for j in jobs]):
            res = fut.result()
            if res.get("ok"):
                _append_account(res); ok += 1
                log(f"✓ {res['email']}  party={(res.get('hostedPartyId') or '')[:30]}…")
            else:
                log(f"✗ {res['email']}: {res.get('why')}")
    log(f"[done] {ok}/{n} akun terdaftar (HTTP)")
    return ok

def _count_accts():
    with _flock():
        return len(json.load(open(STATE))) if os.path.exists(STATE) else 0

def register_until(target, workers=4, chunk=None, log=print, stop=None):
    """Loop register sampai TOTAL akun di accounts.json >= target. Tahan-banting: batch gagal
    di-retry (backoff), hitung ulang dari file (aman paralel/rugi). Return jumlah akun BARU.
    stop: threading.Event opsional untuk berhenti dini."""
    start = _count_accts()
    if start >= target:
        log(f"[skip] sudah {start} akun (>= target {target})"); return 0
    made = 0; streak = 0
    while True:
        if stop is not None and stop.is_set():
            log(f"[stop] dihentikan — total {_count_accts()}, +{made} baru"); break
        cur = _count_accts()
        if cur >= target:
            log(f"[done] target {target} tercapai — total {cur} (+{made} baru)"); break
        need = target - cur
        batch = min(need, chunk) if chunk else need
        log(f"[loop] total {cur}/{target} — register {batch}…")
        ok = register_batch(batch, workers=workers, log=log)
        made += ok
        if ok == 0:
            streak += 1
            wait = min(15 * streak, 120)
            log(f"[warn] batch 0 sukses (streak {streak}) — backoff {wait}s"); time.sleep(wait)
        else:
            streak = 0
    return made

def main():
    if "--selftest" in sys.argv: selftest(); return
    workers = int(os.environ.get("REG_WORKERS", "4"))
    if "--until" in sys.argv:
        i = sys.argv.index("--until")
        target = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit() else 0
        register_until(target, workers=workers); return
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    register_batch(n, workers=workers)

if __name__ == "__main__":
    main()
