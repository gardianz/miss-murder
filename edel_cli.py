#!/usr/bin/env python3
"""
EDEL DESK TERMINAL — CLI interaktif + dashboard gaya Bloomberg untuk Runway Edel.
Fitur: dashboard live, jalankan listing calls (paralel), refresh sesi, status fleet,
       Party ID untuk deposit, pantau settlement.

usage:
  python3 edel_cli.py            # menu interaktif
  python3 edel_cli.py dash       # langsung dashboard live
"""
import sys, os, time, threading, datetime as dt, termios, tty, select
from concurrent.futures import ThreadPoolExecutor, as_completed

import listing_bot as LB
import sender_bot as SB
import register_http as RH
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich.align import Align
from rich import box
import questionary

console = Console()

# ── palet Bloomberg (amber/hijau di gelap) ─────────────────────────────
C_HEAD = "bold black on orange1"
C_KEY = "bright_cyan"; C_OK = "bright_green"; C_WARN = "yellow"; C_BAD = "bright_red"
C_DIM = "grey62"; C_VAL = "bold white"; C_AMBER = "orange1"

# fase listing call → (label pendek, warna) untuk tabel ACCOUNTS
PHASE_SHORT = {
    "idle": ("—", C_DIM), "preparing": ("Preparing", C_AMBER),
    "allocation_pending": ("Alloc pend", C_AMBER), "submitted": ("Submitted", C_KEY),
    "demand_pending": ("DI pending", C_WARN), "final": ("DI final", C_OK), "unknown": ("?", C_DIM),
}

MARKET = {"ts": 0, "listing": None, "demand": []}      # cache market (window + demand index)
FLEET = {"ts": 0, "data": {}}                            # cache live per-akun {email: {...}}
LOG = []                                                  # activity log
SEL = 0                                                   # index akun terpilih di dashboard

def _read_key(timeout=0.15):
    """Baca keypress non-blocking via os.read (RAW fd — hindari buffering sys.stdin yang bikin
    panah salah-baca). Panah dikirim terminal sbg 3 byte atomik b'\\x1b[A'. Return 'up'|'down'|'q'|char|None."""
    fd = sys.stdin.fileno()
    r, _, _ = select.select([fd], [], [], timeout)
    if not r: return None
    try: data = os.read(fd, 8)  # cukup untuk escape sequence terpanjang
    except OSError: return None
    if data in (b"\x1b[A", b"\x1bOA"): return "up"
    if data in (b"\x1b[B", b"\x1bOB"): return "down"
    if data in (b"q", b"Q"): return "q"
    if data == b"\x03": return "\x03"     # Ctrl-C
    if data == b"\x1b": return "esc"       # ESC murni
    return None                             # apa pun lain -> ABAIKAN (jangan keluar)

def logline(msg):
    LOG.append(f"{dt.datetime.now():%H:%M:%S} {msg}")
    del LOG[:-200]

def fmt_amt(x):
    try: return f"{x:,.2f}"
    except Exception: return str(x)

def parse_iso(s):
    try: return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def hms(secs):
    secs = int(max(0, secs)); h = secs // 3600; m = (secs % 3600) // 60; s = secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# ── pengambil data ─────────────────────────────────────────────────────
def any_session_acct(accts):
    for a in LB.targets(accts):
        if LB.session_valid(a): return a
    for a in LB.targets(accts):
        if LB.ensure_session(a, accts): return a
    return None

def refresh_market(accts):
    a = any_session_acct(accts)
    if not a: return
    st, lr = LB.api(a, "GET", "/listing-round")
    if isinstance(lr, dict): MARKET["listing"] = lr
    st, di = LB.api(a, "GET", "/demand-index")
    if isinstance(di, dict): MARKET["demand"] = di.get("rankings", [])
    MARKET["ts"] = time.time()

def refresh_fleet(accts, workers=10, only_session=True):
    ts = LB.targets(accts)
    if only_session:
        ts = [a for a in ts if LB.session_valid(a)]
    def one(a):
        d = {"edelx": None, "round": None, "phase": "idle"}
        b = None
        st, pf = LB.api(a, "GET", "/portfolio")
        if isinstance(pf, dict):
            b = next((x for x in pf.get("balances", []) if x["instrumentId"] == "EDELx"), None)
            if b: d["edelx"] = {k: LB.units(b[k]) for k in ("available", "locked", "staked", "total")}
        st, lr = LB.api(a, "GET", "/listing-round")
        if isinstance(lr, dict):
            rnd = lr.get("round"); d["round"] = rnd.get("status") if rnd else "—"
            d["phase"] = LB.phase_from(lr, b)
        return a["email"], d
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(one, a) for a in ts]):
            em, d = fut.result(); FLEET["data"][em] = d
    FLEET["ts"] = time.time()

# ── render dashboard ───────────────────────────────────────────────────
def render(accts, sel=0):
    now = dt.datetime.now(dt.timezone.utc)
    lst = MARKET.get("listing") or {}
    cw = lst.get("currentWindow", {}); timing = cw.get("timing", {})
    close = parse_iso(timing.get("selectionClosesAt", "")) if timing else None
    settle = timing.get("estimatedNextSettlementAttemptBy", "—")
    phase = "—"; countdown = "—"
    if close:
        rem = (close - now).total_seconds()
        phase = "[bright_green]SELECTION OPEN[/]" if rem > 0 else "[yellow]SETTLING[/]"
        countdown = hms(rem)

    # header
    localt = dt.datetime.now().strftime("%H:%M:%S")
    servt = (lst.get("serverTime", "") or "")[11:19]
    header = Text.assemble(
        ("  RUNWAY ", "bold black on orange1"), ("EDEL DESK TERMINAL  ", "bold black on orange1"),
        (f"   local {localt}", C_DIM), ("   │   ", C_DIM), (f"server {servt} UTC", C_AMBER),
        ("   │   ", C_DIM), (f"data {int(time.time()-MARKET['ts'])}s ago" if MARKET['ts'] else "no data", C_DIM),
    )

    # market panel
    mkt = Table.grid(padding=(0, 1))
    mkt.add_column(style=C_KEY, justify="right"); mkt.add_column(style=C_VAL)
    mkt.add_row("Window", cw.get("roundWindowId", "—")[11:16] + "–" + (timing.get("nextRoundStartsAt","")[11:16] if timing else ""))
    mkt.add_row("Phase", phase)
    mkt.add_row("Closes in", f"[bold orange1]{countdown}[/]")
    mkt.add_row("Settle ~", (settle or "—")[11:19])
    acts = lst.get("actions", {})
    pr = acts.get("prepareRound", {}); mkt.add_row("Prepare", "[green]enabled[/]" if pr.get("enabled") else f"[red]{pr.get('reason','off')}[/]")

    # demand index panel
    dtab = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    dtab.add_column("#", style=C_DIM, width=3, justify="right")
    dtab.add_column("TICKER", style=C_AMBER, width=7)
    dtab.add_column("W/L", style=C_DIM, width=11, justify="right")
    dtab.add_column("SCORE", style=C_OK, justify="right")
    for r in MARKET.get("demand", [])[:14]:
        sc = LB.units(r["score"]) if isinstance(r.get("score"), dict) else r.get("score", 0)
        dtab.add_row(str(r["rank"]), r["ticker"], f"{r.get('wins',0)/1000:.0f}k/{r.get('losses',0)/1000:.0f}k", f"{sc/1e6:,.1f}M")

    # fleet summary
    ts = LB.targets(accts)
    n = len(ts); verif = sum(1 for a in ts if a.get("credVerified") is True)
    sesok = sum(1 for a in ts if LB.session_valid(a))
    def _sum(k): return sum((FLEET["data"].get(a["email"], {}).get("edelx") or {}).get(k, 0) for a in ts)
    edelx_tot = _sum("total"); avail_tot = _sum("available"); locked_tot = _sum("locked"); staked_tot = _sum("staked")
    submitted = sum(1 for a in ts if FLEET["data"].get(a["email"], {}).get("round") == "SUBMITTED")
    calls_tot = sum((a.get("stats") or {}).get("submitted", 0) for a in ts)  # total listing call sukses (kumulatif)
    parts = [
        ("ACCOUNTS ", C_DIM), (f"{n}", C_VAL), ("  VERIFIED ", C_DIM), (f"{verif}", C_OK),
        ("  SESSION OK ", C_DIM), (f"{sesok}", C_OK if sesok else C_WARN),
        ("  EDELx ", C_DIM), (f"{fmt_amt(edelx_tot)}", C_AMBER),
        ("  avail ", C_DIM), (f"{fmt_amt(avail_tot)}", C_OK),
        ("  locked ", C_DIM), (f"{fmt_amt(locked_tot)}", C_WARN),
        ("  staked ", C_DIM), (f"{fmt_amt(staked_tot)}", C_KEY),
        ("  SUB ", C_DIM), (f"{submitted}", C_KEY),
        ("  CALLS✓ ", C_DIM), (f"{calls_tot}", C_OK),
    ]
    au = LB.AUTO_STATE
    if au.get("running"):
        parts += [("   🔁 AUTO ", "bold black on green"), (" sub=", C_DIM), (f"{au['submitted']}", C_OK),
                  (" total=", C_DIM), (f"{au.get('total_submitted',0)}", C_OK),
                  (" locked=", C_DIM), (f"{au['locked']}", C_WARN),
                  (" fail=", C_DIM), (f"{au.get('fail',0)}", C_WARN),
                  (" next=", C_DIM), (f"{int(au.get('next_in',0))}s", C_KEY)]
    summ = Text.assemble(*parts)

    # accounts table (interaktif: baris terpilih di-highlight, ada scroll)
    n = len(ts)
    sel = max(0, min(sel, n - 1)) if n else 0
    ROWS = 14
    off = max(0, min(sel - ROWS // 2, max(0, n - ROWS)))
    atab = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    atab.add_column(" ", width=1)  # marker
    atab.add_column("EMAIL", style=C_VAL, no_wrap=True)
    atab.add_column("PARTY", style=C_DIM, width=11)
    atab.add_column("SES", width=4, justify="center")
    atab.add_column("VF", width=3, justify="center")
    atab.add_column("EDELx av/lock", justify="right", width=15)
    atab.add_column("SUB", width=4, justify="right")
    atab.add_column("FASE", width=13)
    for i in range(off, min(off + ROWS, n)):
        a = ts[i]; em = a["email"]; live = FLEET["data"].get(em, {})
        party = (a.get("hostedPartyId", "").split("edel-user-")[-1][:9]) if a.get("hostedPartyId") else "—"
        ses = "[green]✓[/]" if LB.session_valid(a) else "[red]✗[/]"
        vf = "[green]✓[/]" if a.get("credVerified") is True else "[yellow]?[/]"
        e = live.get("edelx")
        edx = f"{e['available']:.1f}/{(e['staked']+e['locked']):.1f}" if e else "[grey37]—[/]"  # avail / (staked+locked terkunci)
        ph = live.get("phase", "idle")
        flabel, fc = PHASE_SHORT.get(ph, ("—", C_DIM))
        sub = (a.get("stats") or {}).get("submitted", 0)
        subc = f"[{C_OK}]{sub}[/]" if sub else "[grey37]0[/]"
        rstyle = "black on orange1" if i == sel else None  # highlight baris terpilih
        mark = "▶" if i == sel else " "
        atab.add_row(mark, em.split("@")[0][:20], party, ses, vf, edx, subc, f"[{fc}]{flabel}[/]", style=rstyle)
    atab.caption = f"↑/↓ pilih · q keluar    [{sel+1}/{n}]"

    # panel bawah: DETAIL + LOG akun terpilih
    if n:
        a = ts[sel]; em = a["email"]; live = FLEET["data"].get(em, {}); e = live.get("edelx") or {}
        sess = a.get("session") or {}
        exp = sess.get("expires", 0); exp_txt = f"{(exp - time.time())/3600:.1f} jam" if exp > time.time() else "[red]expired[/]"
        d = Table.grid(padding=(0, 2))
        d.add_column(style=C_KEY, justify="right"); d.add_column(style=C_VAL)
        d.add_column(style=C_KEY, justify="right"); d.add_column(style=C_VAL)
        d.add_row("Email", em, "Verified", "[green]ya[/]" if a.get("credVerified") is True else "[yellow]?[/]")
        d.add_row("EDELx avail", f"[orange1]{e.get('available',0):.2f}[/]", "staked", f"{e.get('staked',0):.2f}")
        d.add_row("locked", f"{e.get('locked',0):.2f}", "total", f"{e.get('total',0):.2f}")
        d.add_row("Round", str(live.get("round") or "—"), "Sesi", exp_txt)
        fase = LB.PHASE_LABEL.get(live.get("phase", "idle"), "?")
        sub_sel = (a.get("stats") or {}).get("submitted", 0)
        d.add_row("Fase", f"[orange1]{fase}[/]", "Calls✓", str(sub_sel))
        pid = a.get("hostedPartyId", "—")
        d.add_row("Party ID", f"[grey62]{pid}[/]", "", "")
        logtxt = Text("\n".join(LOG[-5:]) if LOG else "(idle)", style=C_DIM)  # aktivitas terkini (global)
        detail = Group(d, Text("── ACTIVITY ──" + "─" * 46, style="grey30"), logtxt)
        detail_title = f"[bold]ACCOUNT ▶ {em.split('@')[0]}"
    else:
        detail = Text("tak ada akun", style=C_DIM); detail_title = "ACCOUNT"

    lay = Layout()
    lay.split_column(
        Layout(Panel(header, box=box.HEAVY, style="on grey11"), size=3, name="h"),
        Layout(name="top", size=13),
        Layout(Panel(summ, box=box.MINIMAL, style="on grey15"), size=3, name="sum"),
        Layout(Panel(atab, title="[bold]ACCOUNTS", box=box.ROUNDED, border_style=C_AMBER), name="acc"),
        Layout(Panel(detail, title=detail_title, box=box.ROUNDED, border_style=C_KEY), size=11, name="log"),
    )
    lay["top"].split_row(
        Layout(Panel(mkt, title="[bold]MARKET / WINDOW", box=box.ROUNDED, border_style=C_AMBER), name="mkt"),
        Layout(Panel(dtab, title="[bold]DEMAND INDEX  (top 14)", box=box.ROUNDED, border_style=C_AMBER), name="di", ratio=2),
    )
    return lay

# ── aksi ───────────────────────────────────────────────────────────────
def dashboard(accts, auto_stop=None):
    """Dashboard live interaktif. auto_stop=Event: kalau dikasih, tekan q/Ctrl-C set event itu
    (untuk hentikan auto_loop yang jalan di thread lain) sebelum keluar."""
    global SEL
    logline("dashboard dibuka")
    ts = LB.targets(accts)
    stop = threading.Event()
    def bg():
        while not stop.is_set():
            try:
                if time.time() - MARKET["ts"] > 25: refresh_market(accts)
                if time.time() - FLEET["ts"] > 90: refresh_fleet(accts)
                if ts and 0 <= SEL < len(ts):
                    a = ts[SEL]
                    if LB.session_valid(a): FLEET["data"][a["email"]] = SB.account_state(a)
            except Exception as e: logline(f"refresh err: {str(e)[:50]}")
            stop.wait(2)
    threading.Thread(target=bg, daemon=True).start()
    console.print("[dim]memuat data pasar…[/]")
    try: refresh_market(accts)
    except Exception: pass

    def cleanup():
        stop.set()
        if auto_stop is not None: auto_stop.set()  # hentikan auto_loop

    if not sys.stdin.isatty():  # non-TTY (pipe): auto-refresh tanpa keyboard
        try:
            with Live(render(accts, SEL), console=console, screen=True, refresh_per_second=2) as live:
                while not (auto_stop and auto_stop.is_set()):
                    live.update(render(accts, SEL)); time.sleep(1)
        except KeyboardInterrupt: pass
        finally: cleanup()
        return

    fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(render(accts, SEL), console=console, screen=True, refresh_per_second=8) as live:
            while True:
                live.update(render(accts, SEL))
                k = _read_key(0.15)
                if k == "up": SEL = max(0, SEL - 1)
                elif k == "down": SEL = min(len(ts) - 1, SEL + 1)
                elif k in ("q", "esc", "\x03"): break
    except KeyboardInterrupt: pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        cleanup()
        console.print("[dim]keluar dashboard[/]")

def act_run(accts):
    which = questionary.select("Jalankan listing calls untuk:", choices=["Semua akun (sesi valid diutamakan)", "Pilih 1 email", "Batal"]).ask()
    if not which or which == "Batal": return
    emails = None
    if which == "Pilih 1 email":
        em = questionary.text("email:").ask()
        if not em: return
        emails = [em if "@" in em else em + "@weling.web.id"]
    workers = int(questionary.text("paralel workers:", default="8").ask() or "8")
    console.print("[orange1]menjalankan…[/]")
    res = LB.run_all(accts, emails=emails, workers=workers, log=lambda m: (logline(m), console.print(m)))
    ok = sum(1 for v in res.values() if v == "submitted")
    console.print(f"[bold green]selesai: {ok}/{len(res)} submit[/]"); logline(f"run: {ok}/{len(res)} submit")
    questionary.text("enter untuk lanjut").ask()

def act_register(accts):
    cur = len(LB.load_accts())
    mode = questionary.select(
        f"Register akun (sekarang {cur} akun):",
        choices=["Tambah N akun (sekali)", "Loop sampai TOTAL target", "Batal"]).ask()
    if not mode or mode == "Batal": return
    workers = int(questionary.text("paralel workers:", default="4").ask() or "4")
    if mode.startswith("Loop"):
        target = int(questionary.text("TOTAL akun target (loop terus sampai tercapai):",
                                      default=str(cur + 50)).ask() or str(cur))
        console.print(f"[orange1]loop register sampai total {target} akun (Ctrl-C untuk stop)…[/]")
        try:
            made = RH.register_until(target, workers=workers, log=lambda m: (logline(m), console.print(m)))
            console.print(f"[bold green]selesai: +{made} akun baru (total {len(LB.load_accts())})[/]")
        except KeyboardInterrupt:
            console.print(f"[yellow]dihentikan (total {len(LB.load_accts())} akun)[/]")
    else:
        n = int(questionary.text("Jumlah akun baru:", default="5").ask() or "5")
        console.print(f"[orange1]register {n} akun via HTTP…[/]")
        ok = RH.register_batch(n, workers=workers, log=lambda m: (logline(m), console.print(m)))
        console.print(f"[bold green]selesai: {ok}/{n} akun terdaftar[/]")
    questionary.text("enter untuk lanjut").ask()

def act_auto(accts):
    workers = int(questionary.text("paralel workers:", default="16").ask() or "16")
    fast = int(questionary.text("interval CEPAT (detik, saat ada yg menunggu settlement):", default="45").ask() or "45")
    slow = int(questionary.text("interval LAMBAT (detik, saat idle):", default="180").ask() or "180")
    console.print("[orange1]AUTO mode: tampil dashboard live, auto submit tiap window. Tekan q / Ctrl-C untuk berhenti.[/]")
    stop = threading.Event()
    def runner():
        try: LB.auto_loop(accts, workers=workers, poll_slow=slow, poll_fast=fast, log=logline, stop=stop)
        except Exception as e: logline(f"[auto] crash: {str(e)[:80]}")
    th = threading.Thread(target=runner, daemon=True); th.start()
    dashboard(accts, auto_stop=stop)  # tampilan dashboard; q/Ctrl-C -> stop.set()
    stop.set()
    console.print("[orange1]menghentikan auto… (menunggu iterasi berjalan selesai)[/]")
    th.join(timeout=20)
    console.print("[green]auto berhenti.[/]")

def act_sessions(accts):
    ts = LB.targets(accts); need = [a for a in ts if not LB.session_valid(a)]
    console.print(f"[orange1]refresh sesi: {len(need)} akun perlu login…[/]")
    ok = 0
    with ThreadPoolExecutor(max_workers=int(questionary.text('workers:',default='8').ask() or '8')) as ex:
        futs = {ex.submit(LB.refresh_session, a, accts): a for a in need}
        for f in as_completed(futs):
            if f.result(): ok += 1
            console.print(f"  {ok}/{len(need)} ok", end="\r")
    console.print(f"\n[green]sesi valid sekarang: {sum(1 for a in ts if LB.session_valid(a))}/{len(ts)}[/]")
    logline(f"refresh sessions: +{ok}")
    questionary.text("enter untuk lanjut").ask()

def act_status(accts):
    console.print("[orange1]memuat status fleet…[/]")
    refresh_fleet(accts, only_session=False)
    t = Table(box=box.SIMPLE_HEAD)
    for c in ["EMAIL", "SES", "VF", "EDELx av", "staked", "ROUND"]: t.add_column(c)
    for a in LB.targets(accts):
        d = FLEET["data"].get(a["email"], {}); e = d.get("edelx") or {}
        t.add_row(a["email"].split("@")[0], "✓" if LB.session_valid(a) else "✗",
            "✓" if a.get("credVerified") is True else "?",
            f"{e.get('available',0):.1f}", f"{e.get('staked',0):.1f}", str(d.get("round","—")))
    console.print(t); questionary.text("enter untuk lanjut").ask()

def act_partyids(accts):
    ts = LB.targets(accts)
    path = LB.STATE.replace("accounts.json", "party_ids.csv")
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["email", "party_id", "credVerified"])
        for a in ts: w.writerow([a["email"], a.get("hostedPartyId", ""), a.get("credVerified")])
    verif = [a for a in ts if a.get("credVerified") is True]
    console.print(f"[green]party_ids.csv ditulis: {len(ts)} akun ({len(verif)} verified/aman deposit)[/]")
    console.print(f"[dim]{path}[/]")
    questionary.text("enter untuk lanjut").ask()

def act_settle_watch(accts):
    em = questionary.text("email akun pantau:").ask()
    if not em: return
    if "@" not in em: em += "@weling.web.id"
    acct = next((a for a in accts if a["email"] == em), None)
    if not acct: console.print("[red]akun tak ada[/]"); return
    console.print("[orange1]pantau settlement (Ctrl-C berhenti)…[/]")
    try:
        while True:
            st, pf = LB.api(acct, "GET", "/portfolio")
            b = next((x for x in pf.get("balances", []) if x["instrumentId"] == "EDELx"), None) if isinstance(pf, dict) else None
            if b:
                f = lambda k: LB.units(b[k])
                console.print(f"{dt.datetime.now():%H:%M:%S} avail={f('available'):.1f} staked={f('staked'):.1f} total={f('total'):.1f}")
                if f("available") > 0 and f("staked") == 0:
                    console.print("[green]SETTLED — stake released[/]"); break
            time.sleep(30)
    except KeyboardInterrupt: pass

def act_send(accts):
    ts = LB.targets(accts)
    # SENDER: pilih cepat TANPA muat saldo 109 akun (lambat). Saldo dicek saat eksekusi (fase 1 bulk_send).
    load_bal = questionary.confirm("Muat saldo EDELx dulu? (LAMBAT ~semua akun; untuk urut sender by saldo)",
                                   default=False).ask()
    if load_bal:
        console.print("[dim]memuat saldo EDELx…[/]")
        refresh_fleet(accts, only_session=False)
        def av(a): return (FLEET["data"].get(a["email"], {}).get("edelx") or {}).get("available", 0.0)
        ts = sorted(ts, key=av, reverse=True)  # saldo terbanyak di atas
        labels = [f"{a['email'].split('@')[0]:<22} {av(a):>10.2f} EDELx" for a in ts]
    else:
        labels = [a["email"].split("@")[0] for a in ts]
    picked = questionary.checkbox("Pilih akun SENDER (ketik untuk filter; spasi=pilih, enter=lanjut):",
                                  choices=labels).ask()
    if not picked: return
    senders = [ts[labels.index(p)]["email"] for p in picked]
    # TARGET: interaktif
    tmode = questionary.select("Kirim ke:", choices=[
        "Pilih akun tujuan (checkbox interaktif)", "Semua akun lain (auto, skip settlement — LAMBAT)",
        "Ketik email manual", "Batal"]).ask()
    if not tmode or tmode == "Batal": return
    by_prefix = {a["email"].split("@")[0]: a for a in ts}
    if tmode.startswith("Pilih akun tujuan"):
        opts = [p for p in by_prefix if by_prefix[p]["email"] not in senders]
        chosen = questionary.checkbox("Pilih TUJUAN (ketik untuk filter; spasi=pilih):", choices=opts).ask()
        if not chosen: return
        targets = [(by_prefix[c]["email"], by_prefix[c]["hostedPartyId"]) for c in chosen]
    elif tmode.startswith("Semua akun lain"):
        # prioritas saldo kecil/kosong dulu, skip settlement (query per-akun → lambat, tapi user pilih)
        if not FLEET["data"]:
            console.print("[dim]memuat status fleet untuk skip settlement…[/]")
            refresh_fleet(accts, only_session=False)
        targets = SB.build_targets_all(accts, senders, fleet=FLEET["data"], log=lambda m: console.print(f"[dim]{m}[/]"))
    else:
        raw = questionary.text("email tujuan (pisah koma):").ask() or ""
        tos = [e if "@" in e else e + "@weling.web.id" for e in raw.split(",") if e.strip()]
        bye = {a["email"]: a for a in ts}
        targets = [(e, bye[e]["hostedPartyId"]) for e in tos if e in bye]
    if not targets: console.print("[red]tak ada target[/]"); return
    amt_s = questionary.text("Nominal EDELx (fixed '110' atau range '100-120', min 100):", default="110").ask()
    spec = SB._parse_amount(amt_s or "110")
    console.print(f"[orange1]SENDER {len(senders)} → TARGET {len(targets)} | nominal {spec} | min {SB.MIN_WD}[/]")
    if not questionary.confirm(f"Kirim EDELx ke {len(targets)} akun sekarang?", default=False).ask():
        return
    res = SB.bulk_send(accts, senders, targets, spec, log=lambda m: (logline(m), console.print(m)))
    ok = sum(1 for _, s, _ in res if s == "ok")
    console.print(f"[bold green]selesai: {ok}/{len(res)} transfer sukses[/]")
    questionary.text("enter untuk lanjut").ask()

def act_history(accts):
    live = questionary.confirm("Cek saldo LIVE dari server? (lambat, tapi akurat)", default=True).ask()
    console.print("[dim]menghitung riwayat listing call…[/]")
    LB.show_history(accts, live=bool(live))
    questionary.text("enter untuk lanjut").ask()

def set_proxy_mode(off):
    """Set mode proxy runtime (berlaku ke listing/login/register/transfer)."""
    LB.NO_PROXY = off
    RH.NO_PROXY = off

def menu():
    accts = LB.load_accts(); LB._ACCTS = accts
    while True:
        pxlabel = "🌐 Proxy: OFF (koneksi langsung)" if LB.NO_PROXY else "🌐 Proxy: ON (pakai proxy akun)"
        choice = questionary.select(
            "EDEL DESK TERMINAL — pilih:",
            choices=["📊 Dashboard live", "➕ Register Akun (HTTP)", "▶  Jalankan Listing Calls (sekali)",
                     "🔁 Auto Listing (tiap window)", "💸 Kirim EDELx (bulk)", "🔑 Refresh Sesi",
                     "📈 Riwayat Listing Call", "📋 Status Fleet", "🏦 Party IDs (deposit)", "⏳ Pantau Settlement",
                     pxlabel, "❌ Keluar"],
        ).ask()
        if not choice or choice.startswith("❌"): break
        if choice.startswith("🌐"):
            set_proxy_mode(not LB.NO_PROXY)
            console.print(f"[orange1]Proxy sekarang: {'OFF (langsung)' if LB.NO_PROXY else 'ON (proxy akun)'}[/]")
            continue
        accts = LB.load_accts(); LB._ACCTS = accts  # reload state fresh
        if choice.startswith("📊"): dashboard(accts)
        elif choice.startswith("➕"): act_register(accts)
        elif choice.startswith("▶"): act_run(accts)
        elif choice.startswith("🔁"): act_auto(accts)
        elif choice.startswith("💸"): act_send(accts)
        elif choice.startswith("🔑"): act_sessions(accts)
        elif choice.startswith("📈"): act_history(accts)
        elif choice.startswith("📋"): act_status(accts)
        elif choice.startswith("🏦"): act_partyids(accts)
        elif choice.startswith("⏳"): act_settle_watch(accts)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dash":
        a = LB.load_accts(); LB._ACCTS = a; dashboard(a)
    else:
        try: menu()
        except KeyboardInterrupt: console.print("\n[dim]bye[/]")
