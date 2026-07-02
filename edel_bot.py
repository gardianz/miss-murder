#!/usr/bin/env python3
"""
Edel Runway auto-register + login bot
  tempik email -> puppeteer regis (WebAuthn passkey virtual auth) -> /desk
  Passkey credential di-export via CDP (incl. privateKey) -> login ulang bisa

No captcha. No email verify (passkey = auth).
Credential saved: credentialId, privateKey, userHandle, signCount, rpId

usage:
  python3 edel_bot.py [count]          # register N accounts
  python3 edel_bot.py --login [email]  # login ulang (semua atau spesifik email)

signCount logic:
  - Export dari CDP setelah regis = 1 (authenticator sign 1x saat regis)
  - Inject signCount=exported_value, authenticator hasilkan count=exported+1
  - Server cek: received > stored → TRUE
  - Setelah login sukses: stored di server = exported+1, update signCount di file
"""
import os, sys, time, json, random, subprocess, requests

TEMPIK = "https://tempik.weling.web.id/api"
ENGINE = "/home/hermes/garapan/anu-regis/edel-regis/engine"
STATE  = "/home/hermes/garapan/anu-regis/edel-regis/accounts.json"

NAMES = ["Alice","Nova","Kai","Zara","Leo","Mira","Rex","Iris","Dax","Luna","Vic","Ash","Neo","Remy","Skye"]

# email local-part bergaya nama Indonesia: kata1+kata2[+angka], contoh: harimaubukit13, awanbiru
EMAIL_W1 = ["harimau","rambutan","dewi","kuda","awan","pinus","kenari","cakra","putri","bulan",
    "abimanyu","cerita","gunung","galaksi","salak","dimas","gemericik","merak","elang","melati",
    "surya","bayu","teratai","rusa","kabut","ombak","embun","rajawali","kunang","seruni",
    "angsa","kemuning","cempaka","garuda","nusa","samudra","bintang","hujan","fajar","senja"]
EMAIL_W2 = ["bukit","kelabu","padang","nila","biru","kuning","merah","muda","ikhlas","hijau",
    "hangat","kencang","karya","damai","lembah","jingga","perak","emas","pagi","malam",
    "rimba","sunyi","tenang","cerah","indah","permai","abadi","lestari","mekar","ceria"]

def gen_local(existing):
    for _ in range(300):
        w = random.choice(EMAIL_W1) + random.choice(EMAIL_W2)
        if random.random() < 0.6:
            w += str(random.randint(10, 99))
        if w not in existing:
            return w
    return random.choice(EMAIL_W1) + random.choice(EMAIL_W2) + str(random.randint(1000, 9999))

def load_accts():
    return json.load(open(STATE)) if os.path.exists(STATE) else []

def save_accts(accts):
    json.dump(accts, open(STATE, "w"), indent=2)

def load_proxies():
    fp = os.path.expanduser("~/.proxies.txt")
    if not os.path.exists(fp): return []
    return [l.strip() for l in open(fp) if l.strip() and not l.startswith("#")]

def tempik_inbox(local):
    s = requests.get(f"{TEMPIK}/session", timeout=15).json()["sessionId"]
    requests.post(f"{TEMPIK}/inboxes", headers={"x-session-id": s, "Content-Type": "application/json"},
        json={"localPart": local}, timeout=15)
    return s

def run_node(script, *args, timeout=120):
    env = dict(os.environ, NODE_PATH="./node_modules")
    cmd = ["node", script] + list(args)
    r = subprocess.run(cmd, cwd=ENGINE, env=env, capture_output=True, text=True, timeout=timeout)
    line = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if line:
        return json.loads(line[-1])
    return {"raw": r.stdout[:300], "err": r.stderr[:300], "ok": False}

def create():
    proxies = load_proxies()
    proxy = random.choice(proxies) if proxies else None
    existing = {a["email"].split("@")[0] for a in load_accts()}
    local = gen_local(existing)
    email = f"{local}@weling.web.id"
    display = random.choice(NAMES) + str(random.randint(100,999))
    print(f"[+] {email}  name={display}  proxy={(proxy.split('@')[1] if proxy and '@' in proxy else proxy or 'direct')}")

    tempik_inbox(local)
    res = run_node("edel_regis.js", display, email, *([proxy] if proxy else []))

    if res.get("ok"):
        # reject accounts whose exported private key does NOT match the registered public key.
        # credVerified: True=proven good, None=couldn't check (accept), False=bad export (retry)
        if res.get("credVerified") is False or not res.get("credential"):
            return {"email": email, "displayName": display, "ok": False,
                    "detail": {"reason": "cred_verify_failed", "credVerified": res.get("credVerified"),
                               "credCount": res.get("credCount"), "regCount": res.get("regCount")}}
        prof = res.get("profile", {})
        return {
            "email": email,
            "displayName": display,
            "ok": True,
            "profileId": prof.get("id"),
            "hostedPartyId": prof.get("hostedPartyId"),
            "proxy": proxy,
            "finalUrl": res.get("finalUrl"),
            "credVerified": res.get("credVerified"),
            "credential": res.get("credential")  # {credentialId, privateKey, userHandle, signCount, rpId, ...}
        }
    return {"email": email, "displayName": display, "ok": False, "detail": res}

def do_login(acct):
    """Login satu akun. Update signCount di file setelah sukses."""
    cred = acct.get("credential")
    if not cred:
        print(f"  SKIP {acct['email']}: no credential")
        return False

    cred_json = json.dumps(cred)
    proxy = acct.get("proxy")
    res = run_node("edel_login.js", acct["email"], cred_json,
                   *([proxy] if proxy else []), timeout=300)

    print(f"  RESULT: {json.dumps(res)[:300]}")

    if res.get("ok"):
        # update signCount — server stored = old_signCount+1, next login needs old+1
        accts = load_accts()
        for a in accts:
            if a["email"] == acct["email"] and a.get("credential"):
                a["credential"]["signCount"] = a["credential"].get("signCount", 1) + 1
                break
        save_accts(accts)
        print(f"  signCount updated to {acct['credential'].get('signCount',1)+1}")
        return True

    if res.get("error") == "credential_invalid_401":
        print(f"  WARN: 401 credential invalid for {acct['email']} — signCount may be stale")
    return False

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        email_filter = sys.argv[2] if len(sys.argv) > 2 else None
        accts = load_accts()
        targets = [a for a in accts if a.get("ok") and a.get("credential")]
        if email_filter:
            targets = [a for a in targets if a["email"] == email_filter]
        if not targets:
            print("No accounts with credential found")
            return
        for a in targets:
            print(f"\n[login] {a['email']}")
            do_login(a)
        return

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    accts = load_accts()
    for i in range(n):
        print(f"\n=== regis {i+1}/{n} ===")
        attempt = 0
        while True:
            a = create()
            print("  RESULT:", json.dumps(a)[:300])
            if a.get("ok"):
                accts.append(a)
                save_accts(accts)
                break
            attempt += 1
            wait = min(8 * attempt, 60)
            print(f"  retry #{attempt} in {wait}s (server flaky)")
            time.sleep(wait)
        time.sleep(random.randint(4, 9))

    ok = sum(1 for a in accts if a.get("ok"))
    print(f"\n[done] {ok} accounts saved -> {STATE}")

if __name__ == "__main__":
    main()
