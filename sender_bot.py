#!/usr/bin/env python3
"""
Bulk sender EDELx — kirim token dari akun sender ke Party ID akun terdaftar lain.
Fitur: pilih 1+ sender, nominal fixed/range (min 100), AUTO ROTASI sender saat saldo kurang, KEBAL.

Dipakai via edel_cli.py (menu "Kirim EDELx") atau langsung:
  python3 sender_bot.py --send --from a@..,b@.. --to c@..,d@.. --amount 110
  python3 sender_bot.py --send --from a@.. --to-all --amount 100-120 --min-keep 0
"""
import os, sys, json, time, uuid, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import listing_bot as LB

INSTRUMENT = "EDELx"
MIN_WD = float(os.environ.get("MIN_WD", "100"))  # minimum withdraw/transfer per web = 100
TRANSFER_RETRIES = int(os.environ.get("TRANSFER_RETRIES", "5"))  # retry saat kontensi ledger Canton
SETTLE_GAP = float(os.environ.get("SETTLE_GAP", "0.8"))  # jeda antar transfer dari SENDER sama (change-contract siap)

def _err(data): return (data.get("error") or {}).get("code", "") if isinstance(data, dict) else ""

def avail(acct):
    st, pf = LB.api(acct, "GET", "/portfolio")
    if not isinstance(pf, dict): return None
    b = next((x for x in pf.get("balances", []) if x["instrumentId"] == INSTRUMENT), None)
    return LB.units(b["available"]) if b else 0.0

def ensure_send_preapproval(acct, log=print):
    st, pa = LB.api(acct, "GET", "/transfers/preapprovals")
    pas = pa.get("preapprovals", []) if isinstance(pa, dict) else []
    e = next((p for p in pas if p.get("instrumentId") == INSTRUMENT), None)
    if e and e.get("enabled"): return True
    st, r = LB.api(acct, "POST", "/transfers/preapprovals", {"instrumentId": INSTRUMENT})
    ok = st in (200, 201)
    log(f"  [{acct['email']}] enable sending preapproval: {'ok' if ok else st}")
    return ok

# ── Receiving Settings (wallet page) = /transfers/preapprovals (plural, per-token) ──
# Endpoint singular /transfers/preapproval SUDAH MATI (404 ROUTE_NOT_FOUND). Semua receiving
# preapproval sekarang lewat endpoint plural: GET daftar {instrumentId, enabled}, POST {instrumentId}
# untuk enable. instrumentId di sini pakai "CC" (bukan "Amulet" spt di /portfolio). CC default OFF →
# reward CC tak landing sampai receiving CC di-enable.
RECV_INSTRUMENTS = ("EDELx", "CC")  # fallback kalau daftar preapproval kosong

def get_preapprovals(acct):
    """Daftar receiving preapproval per-token: [{instrumentId, enabled}] (= Receiving Settings wallet)."""
    st, pa = LB.api(acct, "GET", "/transfers/preapprovals")
    return pa.get("preapprovals", []) if isinstance(pa, dict) else []

def enable_receiving(acct, instrument, log=print):
    """Aktifkan RECEIVING satu token. POST /transfers/preapprovals {instrumentId}.
    200/201/202 = ok (202 = async on-chain accepted, mirip transfer). Gagal → log status ASLI
    (bukan cuma 'ERR'): 'ERR <detail>' = api mentok API_DEADLINE (biasanya contention on-chain/429)."""
    st, r = LB.api(acct, "POST", "/transfers/preapprovals", {"instrumentId": instrument})
    ok = st in (200, 201, 202)
    if not ok:
        detail = _err(r) or (r if isinstance(r, str) else "")
        log(f"  [{acct['email']}] enable receiving {instrument}: {st} {detail}".rstrip())
    return ok

def ensure_recv_preapproval(acct, log=print, instrument=INSTRUMENT):
    """Pastikan receiving 1 token (default EDELx) aktif di penerima (butuh sesi penerima)."""
    if not LB.ensure_session(acct, LB._ACCTS or [acct]): return False
    pas = get_preapprovals(acct)
    e = next((p for p in pas if p.get("instrumentId") == instrument), None)
    if e and e.get("enabled"): return True
    return enable_receiving(acct, instrument, log=log)

def ensure_receiving_all(acct, log=print):
    """Aktifkan receiving SEMUA token yang belum enabled (EDELx, CC, dst). Return (n_aktif, n_total).
    CC default OFF → tanpa ini reward CC tak masuk."""
    pas = get_preapprovals(acct)
    if not pas:  # akun baru mungkin belum ada entry → paksa set default
        pas = [{"instrumentId": i, "enabled": False} for i in RECV_INSTRUMENTS]
    total = len(pas); nafter = 0
    for p in pas:
        if p.get("enabled") or enable_receiving(acct, p.get("instrumentId"), log=log):
            nafter += 1
    return nafter, total

PREAPPROVE_WORKERS = int(os.environ.get("PREAPPROVE_WORKERS", "8"))  # POST preapproval = tulis ledger; jgn terlalu banyak (contention)
PREAPPROVE_ROUNDS  = int(os.environ.get("PREAPPROVE_ROUNDS", "4"))   # ronde re-sweep akun yg belum penuh
PREAPPROVE_GAP     = float(os.environ.get("PREAPPROVE_GAP", "6"))    # jeda antar ronde (biar contention on-chain reda)

def enable_all_preapprovals(accts, emails=None, workers=PREAPPROVE_WORKERS,
                            rounds=PREAPPROVE_ROUNDS, gap=PREAPPROVE_GAP, log=print):
    """Bulk: aktifkan receiving SEMUA token untuk banyak akun paralel (butuh sesi tiap akun).
    RE-SWEEP: akun yang belum penuh di-retry sampai `rounds` ronde (jeda `gap` detik antar ronde) —
    kegagalan CC biasanya transient (contention on-chain / 429 saat login+POST massal serempak).
    emails=None → semua target. Return list (email, n_aktif, n_total) hasil TERBAIK per akun."""
    ts = LB.targets(accts)
    if emails is not None:
        want = set(emails); ts = [a for a in ts if a["email"] in want]
    by_email = {a["email"]: a for a in ts}
    best = {}                     # email -> (n_aktif, n_total) terbaik sejauh ini
    pending = [a["email"] for a in ts]
    def one(em):
        a = by_email[em]
        if not LB.ensure_session(a, accts):
            log(f"  [{em}] sesi mati / login gagal — retry ronde berikut"); return (em, 0, 0)
        nn, tot = ensure_receiving_all(a, log=log)
        log(f"  [{em}] receiving {nn}/{tot} token aktif")
        return (em, nn, tot)
    for rnd in range(1, rounds + 1):
        if not pending: break
        if rnd > 1:
            log(f"  ↻ ronde {rnd}/{rounds}: retry {len(pending)} akun belum penuh (jeda {gap:.0f}s)…")
            time.sleep(gap)
        nxt = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(one, em) for em in pending]):
                em, nn, tot = fut.result()
                prev = best.get(em, (0, 0))
                if (nn, tot) > prev or em not in best: best[em] = (nn, tot)
                full = tot and nn == tot
                if not full: nxt.append(em)
        pending = nxt
    return [(em, nn, tot) for em, (nn, tot) in best.items()]

def transfer(sender, to_party, amount, idem, ref=None, log=print):
    """Satu transfer. Return status: ok | recv_needed | insufficient | send_preapproval | fail."""
    body = {"instrumentId": INSTRUMENT, "amount": f"{amount:.4f}".rstrip("0").rstrip("."),
            "toPartyId": to_party, "idempotencyKey": idem}
    if ref: body["reference"] = ref
    st, r = LB.api(sender, "POST", "/transfers", body)
    code = _err(r).lower()
    if st in (200, 201, 202): return "ok"  # 202 Accepted = transfer diproses (async)
    if "insufficient" in code or (st == 409 and "balance" in code): return "insufficient"
    if code == "missing_preapproval": return "send_preapproval"
    if "recipient" in code or "receiving" in code or "utility_transfer_preapproval_required" in code:
        return "recv_needed"
    # KONTENSI LEDGER Canton: holding contract sender lagi dipakai transfer lain (in-flight) atau referensi
    # basi (sudah diarsip transfer sebelumnya). RETRYABLE — cukup serial per sender + tunggu change-contract.
    if ("locked_contract" in code or "in_flight" in code or "inactive_contract" in code
            or "contract_not_found" in code):
        return "locked_contract"
    log(f"  [{sender['email']}] transfer gagal: {st} {code}")
    return "fail"

def account_state(acct):
    """Return {'edelx':{available,locked,staked,total}, 'round':status}."""
    d = {"edelx": None, "round": None}
    st, pf = LB.api(acct, "GET", "/portfolio")
    if isinstance(pf, dict):
        b = next((x for x in pf.get("balances", []) if x["instrumentId"] == INSTRUMENT), None)
        if b: d["edelx"] = {k: LB.units(b[k]) for k in ("available", "locked", "staked", "total")}
    st, lr = LB.api(acct, "GET", "/listing-round")
    if isinstance(lr, dict):
        rnd = lr.get("round"); d["round"] = rnd.get("status") if rnd else None
    return d

def build_targets_all(accts, sender_emails, fleet=None, workers=10, log=print):
    """Target 'semua akun lain' dengan PRIORITAS: saldo terkecil/kosong dulu, SKIP yang sedang
    settlement (ada stake locked/staked atau round SUBMITTED). Return list (email, party_id)."""
    LB._ACCTS = accts
    cand = [a for a in LB.targets(accts)
            if a["email"] not in sender_emails and a.get("hostedPartyId") and a.get("credVerified") is True]
    data = dict(fleet or {})
    missing = [a for a in cand if a["email"] not in data]
    if missing:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(account_state, a): a for a in missing}
            for f in as_completed(futs):
                a = futs[f]
                try: data[a["email"]] = f.result()
                except Exception: data[a["email"]] = {}
    elig, skip = [], 0
    for a in cand:
        s = data.get(a["email"]) or {}; e = s.get("edelx") or {}
        busy = (e.get("locked", 0) + e.get("staked", 0)) > 0 or s.get("round") == "SUBMITTED"
        if busy: skip += 1; continue
        elig.append((a, e.get("available", 0.0)))
    elig.sort(key=lambda x: x[1])  # saldo naik: kosong/terkecil diprioritaskan
    log(f"target: {len(elig)} eligible (kosong/kecil dulu) | {skip} skip (settlement/staked)")
    return [(a["email"], a["hostedPartyId"]) for a, _ in elig]

def resolve_amount(spec):
    """spec: {'mode':'fixed','value':110} | {'mode':'range','min':100,'max':120}"""
    if spec["mode"] == "fixed":
        amt = float(spec["value"])
    else:
        amt = random.uniform(float(spec["min"]), float(spec["max"]))
    amt = round(amt, 4)
    return max(amt, MIN_WD)

def bulk_send(accts, sender_emails, targets, amount_spec, ensure_recv=True, workers=6, log=print):
    """
    targets: list of (email_or_None, party_id). Rotasi sender saat saldo kurang.
    Fase 1: siapkan sesi/preapproval + saldo sender.  Fase 2: ALOKASI greedy (tanpa network,
    rotasi sender per target).  Fase 3: eksekusi transfer PARALEL. Return list hasil per target.
    """
    LB._ACCTS = accts
    by_email = {a["email"]: a for a in accts}
    senders = [by_email[e] for e in sender_emails if e in by_email]
    if not senders: log("tak ada sender valid"); return []
    # Fase 1: sesi + preapproval + saldo (paralel)
    bal = {}
    def prep(s):
        if not LB.ensure_session(s, accts): return None
        ensure_send_preapproval(s, log)
        return s["email"], (avail(s) or 0.0)
    with ThreadPoolExecutor(max_workers=min(workers, len(senders))) as ex:
        for fut in as_completed([ex.submit(prep, s) for s in senders]):
            r = fut.result()
            if r: bal[r[0]] = r[1]; log(f"  sender {r[0].split('@')[0]}: {r[1]:.2f} EDELx")
    # Fase 2: alokasi (tanpa network) — pakai saldo di-memori
    plan, results = [], []
    if amount_spec.get("mode") == "all":
        # KURAS: tiap sender kirim SELURUH saldo (dikurangi min_keep) ke target (rotasi round-robin).
        min_keep = float(amount_spec.get("min_keep", 0))
        if not targets: log("tak ada target"); return []
        i = 0
        for se in list(bal.keys()):
            amt = round(bal[se] - min_keep, 4)
            if amt < MIN_WD:  # sisa < min transfer (100) → tak bisa kirim
                log(f"  skip {se.split('@')[0]}: saldo {bal[se]:.2f} < {MIN_WD:.0f}+keep"); continue
            tgt_email, party = targets[i % len(targets)]; i += 1
            bal[se] -= amt
            plan.append((se, tgt_email, party, amt, f"send:{uuid.uuid4()}"))
        log(f"kuras: {len(plan)} sender → {len(targets)} target (min_keep={min_keep:.2f})")
    else:
        for tgt_email, party in targets:
            amt = resolve_amount(amount_spec)
            cand = sorted([e for e in bal if bal[e] >= amt], key=lambda e: bal[e], reverse=True)
            if not cand:
                log(f"  → {(tgt_email or party)[:24]}: STOP, semua sender saldo < {amt:.2f}")
                results.append((tgt_email or party, "no_sender", amt)); break
            se = cand[0]; bal[se] -= amt
            plan.append((se, tgt_email, party, amt, f"send:{uuid.uuid4()}"))
    # Fase 3: eksekusi. Transfer dari SENDER yang SAMA harus SERIAL (Canton = holding contract tunggal;
    # paralel → rebutan contract → local_verdict_locked_contracts). Paralelkan ANTAR sender saja.
    def one(job):
        se, tgt_email, party, amt, idem = job
        sender = by_email[se]
        if ensure_recv and tgt_email and tgt_email in by_email:
            ensure_recv_preapproval(by_email[tgt_email], log)
        status = "fail"
        for attempt in range(TRANSFER_RETRIES):
            status = transfer(sender, party, amt, idem, log=log)
            if status == "send_preapproval":
                ensure_send_preapproval(sender, log); status = transfer(sender, party, amt, idem, log=log)
            if status == "recv_needed" and tgt_email and tgt_email in by_email:
                if ensure_recv_preapproval(by_email[tgt_email], log):
                    status = transfer(sender, party, amt, idem, log=log)
            if status == "locked_contract":  # kontensi ledger — tunggu change-contract settle, ULANG (idem sama)
                time.sleep(SETTLE_GAP + attempt * 0.7)
                continue
            break
        tag = (tgt_email or party).split("@")[0][:18]
        if status == "ok": log(f"  ✓ {se.split('@')[0]} → {tag}  {amt:.2f} EDELx")
        else: log(f"  ✗ {se.split('@')[0]} → {tag}: {status}")
        return (tgt_email or party, status, amt)
    def run_sender(jobs):
        out = []
        for j in jobs:
            out.append(one(j))
            time.sleep(SETTLE_GAP)  # beri jeda agar change-contract sender siap sebelum transfer berikut
        return out
    if plan:
        from collections import defaultdict
        by_sender = defaultdict(list)
        for j in plan: by_sender[j[0]].append(j)
        log(f"eksekusi: {len(plan)} transfer, {len(by_sender)} sender (serial per sender, paralel antar sender)")
        with ThreadPoolExecutor(max_workers=min(workers, len(by_sender))) as ex:
            for fut in as_completed([ex.submit(run_sender, js) for js in by_sender.values()]):
                results.extend(fut.result())
    ok = sum(1 for _, s, _ in results if s == "ok")
    log(f"[selesai] {ok}/{len(results)} transfer sukses")
    return results

# ── CLI langsung ───────────────────────────────────────────────────────
def _parse_amount(s):
    s = (s or "").strip()
    if s.lower() in ("all", "semua", "max", "kuras"):
        return {"mode": "all", "min_keep": 0}
    if "-" in s:
        lo, hi = s.split("-"); return {"mode": "range", "min": float(lo), "max": float(hi)}
    return {"mode": "fixed", "value": float(s)}

def main():
    args = sys.argv[1:]
    if "--send" not in args: print(__doc__); return
    accts = LB.load_accts(); LB._ACCTS = accts
    def val(flag):
        return args[args.index(flag) + 1] if flag in args else None
    frm = (val("--from") or "").split(",") if val("--from") else []
    frm = [e if "@" in e else e + "@weling.web.id" for e in frm if e]
    amount_spec = _parse_amount(val("--amount") or "110")
    ts = LB.targets(accts)
    if "--to-all" in args:
        targets = build_targets_all(accts, frm)  # prioritas saldo kecil, skip settlement
    else:
        tos = [e if "@" in e else e + "@weling.web.id" for e in (val("--to") or "").split(",") if e]
        bye = {a["email"]: a for a in ts}
        targets = [(e, bye[e]["hostedPartyId"]) for e in tos if e in bye]
    print(f"sender={len(frm)} target={len(targets)} amount={amount_spec}")
    bulk_send(accts, frm, targets, amount_spec)

if __name__ == "__main__":
    main()
