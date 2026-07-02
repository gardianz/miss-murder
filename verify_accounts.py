#!/usr/bin/env python3
"""Verifikasi akun via login HTTP (bukti private key benar). Paralel.
  ok  -> login 200: set credVerified=True + simpan session
  bad -> login 401 (Could not verify): private key salah -> credVerified=False (perlu register ulang)
  down-> server 5xx berulang: biarkan (retry lain waktu)
usage: python3 verify_accounts.py [--all]   (default: hanya credVerified != True)
"""
import sys, json, time, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import listing_bot as LB
from cryptography.hazmat.primitives import serialization

def verify(acct, tries=6):
    cred = acct.get("credential") or {}
    if not cred.get("privateKey"): return "bad", None
    email = acct["email"]; proxy = acct.get("proxy")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0", "Origin": LB.ORIGIN, "Referer": LB.ORIGIN + "/login"})
    def post(path, body):
        for _ in range(tries):
            try:
                r = s.post(LB.BASE + path, json=body, proxies=proxies, timeout=25)
                if r.status_code < 500: return r
            except Exception: pass
            time.sleep(2)
        return None
    r = post("/auth/login/start", {"email": email})
    if not r or r.status_code != 200: return "down", None
    opts = r.json()["options"]
    cd = json.dumps({"type": "webauthn.get", "challenge": opts["challenge"],
        "origin": LB.ORIGIN, "crossOrigin": False}, separators=(",", ":")).encode()
    ad = hashlib.sha256(opts["rpId"].encode()).digest() + bytes([0x05]) + int(time.time()).to_bytes(4, "big")
    key = serialization.load_der_private_key(LB._std_dec(cred["privateKey"]), password=None)
    sig = key.sign(ad + hashlib.sha256(cd).digest())
    body = {"response": {"id": opts["allowCredentials"][0]["id"], "rawId": opts["allowCredentials"][0]["id"],
        "response": {"authenticatorData": LB._b64url(ad), "clientDataJSON": LB._b64url(cd),
            "signature": LB._b64url(sig), "userHandle": LB._b64url(LB._std_dec(cred["userHandle"]))},
        "type": "public-key", "clientExtensionResults": {}, "authenticatorAttachment": "platform"}}
    r = post("/auth/login/finish", body)
    if not r: return "down", None
    if r.status_code == 200:
        cobj = next((c for c in s.cookies if c.name == "edel_session"), None)
        sess = {"value": cobj.value, "expires": cobj.expires or time.time() + 12 * 3600} if cobj else None
        return "ok", sess
    if r.status_code in (400, 401, 403): return "bad", None
    return "down", None

def main():
    all_mode = "--all" in sys.argv
    accts = LB.load_accts()
    ts = [a for a in LB.targets(accts) if all_mode or a.get("credVerified") is not True]
    print(f"verifikasi {len(ts)} akun (paralel)...", flush=True)
    res = {"ok": 0, "bad": 0, "down": 0}
    bad_list = []
    def one(a):
        try: return a, *verify(a)
        except Exception as e: return a, "down", None
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(one, a) for a in ts]):
            a, status, sess = fut.result()
            done += 1
            if status == "ok":
                LB.update_account(a["email"], lambda x: (x.__setitem__("credVerified", True), x.__setitem__("session", sess) if sess else None))
            elif status == "bad":
                LB.update_account(a["email"], lambda x: x.__setitem__("credVerified", False))
                bad_list.append(a["email"])
            res[status] += 1
            print(f"  [{done}/{len(ts)}] {a['email'].split('@')[0]:<22} -> {status.upper()}", flush=True)
    print(f"\n[hasil] OK={res['ok']} BAD={res['bad']} DOWN={res['down']}")
    if bad_list:
        print("BAD (private key salah, perlu register ulang):")
        for e in bad_list: print("  -", e)
    if res["down"]:
        print(f"DOWN={res['down']} (server flaky saat cek — jalankan ulang untuk yang belum True)")

if __name__ == "__main__":
    main()
