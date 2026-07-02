#!/usr/bin/env python3
"""
Edel Runway batch regis — loop until 1 account registers, save to file
Tunggu server stabil, lalu regis terus batch
"""
import os, sys, time, json, random, subprocess

ENGINE = "/home/hermes/garapan/anu-regis/edel-regis/engine"
STATE  = "/home/hermes/garapan/anu-regis/edel-regis/accounts.json"
PROXIES = [l.strip() for l in open(os.path.expanduser("~/.proxies.txt")) if l.strip() and not l.startswith("#")]

def run_node(script, *args):
    env = dict(os.environ, NODE_PATH="./node_modules")
    cmd = ["node", script] + list(args)
    r = subprocess.run(cmd, cwd=ENGINE, env=env, capture_output=True, text=True, timeout=120)
    for l in r.stdout.splitlines():
        if l.startswith("{"):
            try: return json.loads(l)
            except: pass
    return {"ok": False, "raw": r.stdout[:200], "err": r.stderr[:200]}

def create():
    """Regis 1 akun baru"""
    import random as rnd
    local = f"edel{rnd.randint(10000,99999)}{int(time.time())%10000}"
    email = f"{local}@weling.web.id"
    display = rnd.choice(["Alice","Nova","Kai","Zara","Leo","Mira","Rex","Iris","Dax","Luna","Vic","Ash","Neo","Remy","Skye"]) + str(rnd.randint(100,999))
    proxy = rnd.choice(PROXIES) if PROXIES else None

    print(f"[{time.strftime('%H:%M:%S')}] {email} -> {display}", end="")
    if proxy:
        print(f"  proxy={proxy.split('@')[1].split(':')[0] if '@' in proxy else proxy}")
    else:
        print()

    # tempik inbox
    try:
        import requests
        s = requests.get("https://tempik.weling.web.id/api/session", timeout=10).json()["sessionId"]
        requests.post("https://tempik.weling.web.id/api/inboxes", headers={"x-session-id": s, "Content-Type": "application/json"}, json={"localPart": local}, timeout=10)
    except:
        pass

    res = run_node("edel_regis.js", display, email, *([proxy] if proxy else []))
    return res

def main():
    print(f"Edel regis bot — akun di {STATE}")
    print(f"Proxies: {len(PROXIES)}", flush=True)

    accts = json.load(open(STATE)) if os.path.exists(STATE) else []
    attempt = 0
    while True:
        attempt += 1
        r = create()
        if r.get("ok"):
            # export credential
            cred = r.get("credential")
            if cred:
                r["credential"] = cred
            accts.append(r)
            json.dump(accts, open(STATE, "w"), indent=2)
            print(f"\n  >>> {r['email']}  DESK OK  profileId={r.get('profileId','-')}")
            break
        else:
            err = r.get("startStatus", "?") or r.get("err", r.get("raw",""))
            print(f"  fail: {err}", end="")
            if "502" in str(err) or "504" in str(err):
                print(" (server flaky)")
            else:
                print()
        if attempt % 5 == 0:
            sys.stdout.flush()
        time.sleep(min(5 * attempt, 30))

    print(f"\nTotal: {len(accts)} akun")
    for a in accts:
        print(f"  {a['email']}  ok={a.get('ok')}")

if __name__ == "__main__":
    main()