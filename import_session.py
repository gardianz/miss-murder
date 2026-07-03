#!/usr/bin/env python3
"""
Import akun MANUAL (login browser) ke accounts.json — dari JSON hasil Chrome extension.

Akun manual = punya cookie session TAPI tanpa private key (passkey browser tak bisa
di-ekspor). Bot pakai cookie selama hidup (~12 jam). Cookie mati -> ekspor ulang dari
Chrome (bot TIDAK bisa auto-relogin karena tak punya private key).

usage:
  python3 import_session.py                 # tempel JSON, akhiri Ctrl-D
  python3 import_session.py akun.json       # dari file
  echo '<json>' | python3 import_session.py # dari pipe

Terima 1 objek akun ATAU list objek. Dedup by email:
- kalau email sudah ada & punya credential (akun bot) -> HANYA update cookie session.
- kalau email baru / manual -> tambah/replace.
"""
import os, sys, json, time, fcntl
from contextlib import contextmanager

BASEDIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("EDEL_STATE") or os.path.join(BASEDIR, "accounts.json")


@contextmanager
def _flock():
    f = open(STATE + ".lock", "w")
    try: fcntl.flock(f, fcntl.LOCK_EX); yield
    finally: fcntl.flock(f, fcntl.LOCK_UN); f.close()


def _read_input():
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        return open(sys.argv[1]).read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Tempel JSON dari extension, lalu Ctrl-D:")
    return sys.stdin.read()


def _validate(a):
    if not isinstance(a, dict): return "bukan objek"
    if not a.get("email") or "ISI_EMAIL" in a.get("email", ""): return "email kosong/placeholder"
    sess = a.get("session") or {}
    if not sess.get("value"): return "tak ada session.value"
    if sess.get("expires", 0) < time.time(): return "cookie sudah kedaluwarsa"
    return None


def main():
    raw = _read_input().strip()
    if not raw:
        print("input kosong"); return
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"JSON invalid: {e}"); return
    items = data if isinstance(data, list) else [data]

    with _flock():
        accts = json.load(open(STATE)) if os.path.exists(STATE) else []
        by_email = {a.get("email"): a for a in accts}
        added = updated = skipped = 0
        for a in items:
            why = _validate(a)
            if why:
                print(f"✗ SKIP {a.get('email','?')}: {why}"); skipped += 1; continue
            a.setdefault("manual", True)
            a.setdefault("ok", True)
            em = a["email"]
            cur = by_email.get(em)
            if cur is None:
                accts.append(a); by_email[em] = a; added += 1
                print(f"✓ TAMBAH {em} (manual, cookie ~{int((a['session']['expires']-time.time())/60)} mnt)")
            elif cur.get("credential", {}).get("privateKey"):
                # akun BOT sudah ada -> jangan timpa key, cuma segarkan cookie
                cur["session"] = a["session"]; updated += 1
                print(f"↻ UPDATE cookie {em} (akun bot, key dijaga)")
            else:
                # akun manual lama -> replace penuh (cookie & meta baru)
                cur.update(a); updated += 1
                print(f"↻ UPDATE {em} (manual)")
        if added or updated:
            tmp = STATE + ".tmp"
            json.dump(accts, open(tmp, "w"), indent=2)
            os.replace(tmp, STATE)
    print(f"\n[done] +{added} baru, {updated} update, {skipped} skip → {STATE}")
    print("Jalankan bot: python3 listing_bot.py --status  (cek akun manual terbaca)")


if __name__ == "__main__":
    main()
