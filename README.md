# Edel Runway Desk — Bot Otomatisasi

Bot untuk **register akun massal**, **login**, dan **otomatisasi Listing Calls** di
[runway.edel.finance](https://runway.edel.finance) menggunakan passkey (WebAuthn) virtual.

Semua otentikasi berbasis **passkey** (tidak ada password / verifikasi email). Kunci privat tiap
akun disimpan lokal dan dipakai untuk login berulang.

---

## Daftar Isi
1. [Konsep singkat](#konsep-singkat)
2. [Struktur file](#struktur-file)
3. [Data akun (accounts.json)](#data-akun)
4. [Setup / Instalasi (mesin baru)](#setup--instalasi-mesin-baru)
5. [Cara pakai](#cara-pakai)
5. [Alur Listing Calls](#alur-listing-calls)
6. [Sesi & keandalan](#sesi--keandalan)
7. [Penanganan kondisi khusus](#penanganan-kondisi-khusus)
8. [Troubleshooting](#troubleshooting)

---

## Konsep singkat

- **Register**: buka `/register` di Chrome headless (puppeteer) dengan *virtual authenticator* (CDP
  WebAuthn). Server membuat passkey; bot meng-export **kunci privat Ed25519** + verifikasi kripto
  bahwa kunci itu benar-benar pasangan kunci publik yang diregister.
- **Login**: **tanpa browser** — bot membuat WebAuthn assertion sendiri (tanda tangan Ed25519 via
  Python), kirim ke `/auth/login/start` → `/auth/login/finish`, dapat cookie sesi `edel_session`.
- **Operasi** (portfolio, listing calls, dll): HTTP biasa (`requests`) memakai cookie sesi + proxy.
- **Listing Calls**: pilih 1 dari 2 saham per "call" berdasarkan **Demand Index**, lalu submit.

> **PENTING:** tidak ada recovery. Kalau `privateKey` sebuah akun hilang, akun itu tak bisa diakses
> selamanya (tak ada reset email / tambah passkey). **Backup `accounts.json` secara berkala.**

---

## Struktur file

| File | Fungsi |
|------|--------|
| `edel_bot.py` | Register akun massal + login (via puppeteer). `python3 edel_bot.py N` |
| `engine/edel_regis.js` | Puppeteer: register 1 akun + export & verifikasi credential |
| `engine/edel_login.js` | Puppeteer: login (dipakai untuk debug; login utama sudah HTTP) |
| `engine/get_session.js` | Puppeteer: login → ambil cookie sesi (fallback) |
| `run_until_100.sh` | Loop register sampai target akun tercapai (dipakai via `screen`) |
| `listing_bot.py` | **Inti**: login HTTP, sesi, Listing Calls, paralel, file-lock |
| `sender_bot.py` | **Bulk sender EDELx** antar akun (rotasi sender, min 100) |
| `edel_cli.py` | **CLI interaktif + dashboard** gaya Bloomberg |
| `verify_accounts.py` | Verifikasi semua akun bisa login (tandai `credVerified`) |
| `accounts.json` | Database akun (credential, party id, sesi) — **file kritis** |
| `party_ids.csv` | Daftar Party ID untuk deposit EDELx |
| `~/.proxies.txt` | Daftar proxy (format `http://user:pass@host:port`, satu per baris) |

---

## Data akun

Tiap akun di `accounts.json`:

```json
{
  "email": "namaunik12@weling.web.id",
  "displayName": "Alice526",
  "ok": true,
  "hostedPartyId": "edel-user-<hex>::1220<hash>",   // = Party ID untuk deposit
  "credVerified": true,                              // true = kunci privat terbukti benar
  "proxy": "http://user:pass@host:port",
  "credential": {
    "credentialId": "...",
    "privateKey": "...",    // KUNCI — tak bisa direcover kalau hilang
    "publicKey": "...",     // untuk verifikasi offline
    "userHandle": "...",
    "rpId": "edel.finance",
    "signCount": 1
  },
  "session": { "value": "<cookie edel_session>", "expires": 1783000000 }
}
```

- **Party ID** (`hostedPartyId`) = alamat untuk deposit EDELx. Sama dengan yang muncul di halaman
  `/profile` (tombol "Reveal Party ID").
- `credVerified: true` → aman dipakai / dideposit. `false` → kunci salah (register ulang).

---

## Setup / Instalasi (mesin baru)

Clone repo, install dependency, siapkan data. Path akun **otomatis relatif** ke lokasi script
(tak lagi hardcode) — jalankan dari folder repo mana pun.

```bash
# 1. clone (repo privat)
git clone https://github.com/gardianz/miss-murder.git
cd miss-murder

# 2. install dependency Python (requests, cryptography, rich, questionary)
pip3 install -r requirements.txt
#   Ubuntu baru kena "externally-managed-environment":
pip3 install --break-system-packages -r requirements.txt
#   atau pakai venv (disarankan):
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 3. siapkan data akun — accounts.json TIDAK ikut repo (gitignore, berisi private key)
#    a) SALIN dari mesin lama:  scp user@vps-lama:.../accounts.json .
#    b) atau register baru:     python3 register_http.py 10

# 4. (opsional) proxy — salin daftar proxy
cp .proxies.txt.example ~/.proxies.txt   # lalu isi proxy asli, atau jalan tanpa proxy (lihat di bawah)

# 5. jalankan
python3 edel_cli.py            # menu interaktif + dashboard
```

**Catatan penting:**
- `accounts.json`, `~/.proxies.txt`, `party_ids.csv` **tidak ada di repo** (rahasia) — wajib disalin/dibuat manual.
- Path accounts.json default = folder repo. Override dengan env `EDEL_STATE=/path/ke/accounts.json`.
- **Tanpa proxy:** set `NO_PROXY=1` (mis. `NO_PROXY=1 python3 listing_bot.py --auto`) atau
  toggle di menu CLI **🌐 Proxy: ON/OFF**. Ini mengabaikan proxy per-akun & pool → koneksi langsung.

### Daemon (jalan terus di background)
```bash
# auto listing tiap window (poll cepat 25s, hard-retry 6x)
screen -dmS listing bash -c 'HTTP_READ_TO=20 WORKERS=6 AUTO_POLL_FAST=25 HARD_RETRY=6 python3 -u listing_bot.py --auto > auto.log 2>&1'
screen -r listing     # lihat  (Ctrl-A D keluar)
screen -S listing -X quit   # stop
```

---

## Cara pakai

### 1. Register akun
**Full HTTP (tanpa browser — cepat & andal, disarankan):**
```bash
python3 register_http.py 10            # register 10 akun (paralel)
REG_WORKERS=8 python3 register_http.py 50
# atau CLI menu: ➕ Register Akun (HTTP)
```
Register HTTP membuat keypair Ed25519 sendiri + attestation "none" (server mendukung alg -8).
Akun langsung `credVerified=true` (keypair kita, pasti cocok). Tak butuh Chrome/puppeteer.

**Cara lama (puppeteer, masih ada sebagai fallback):**
```bash
python3 edel_bot.py 10
screen -dmS edelreg bash -c './run_until_100.sh 2>&1 | tee -a register_100.log'
```

### 2. Verifikasi akun bisa login
```bash
python3 verify_accounts.py             # cek akun yang belum credVerified
python3 verify_accounts.py --all       # cek ulang semua
```
Hasil: `OK` (bisa login), `BAD` (kunci salah, register ulang), `DOWN` (server flaky, ulangi).

### 3. Ambil Party ID untuk deposit
```bash
python3 listing_bot.py --status        # ringkas EDELx + status round
# party_ids.csv otomatis dibuat lewat menu CLI "Party IDs"
```
Deposit EDELx ke Party ID akun (hanya yang `credVerified=true`).

### 4. Jalankan Listing Calls
```bash
python3 listing_bot.py --run                 # SEKALI, semua akun (paralel)
python3 listing_bot.py --run <email>         # 1 akun
WORKERS=16 python3 listing_bot.py --run      # atur jumlah paralel
MIN_EDELX=5 python3 listing_bot.py --run     # skip akun dgn EDELx < 5
```

**Auto (otomatis tiap window):**
```bash
python3 listing_bot.py --auto                       # loop terus, submit tiap window
AUTO_POLL=120 WORKERS=16 python3 listing_bot.py --auto
# jalan permanen di background (tahan mati sesi):
screen -dmS edelauto python3 listing_bot.py --auto
```
Mode auto menjalankan listing calls untuk semua akun ber-EDELx yang belum submit. Aman dipanggil
berulang (akun yang sudah `SUBMITTED` atau EDELx=0 dilewati). Akun yang EDELx-nya baru lepas dari
settlement langsung ter-submit di **window yang sedang berjalan** pada pengecekan berikutnya.

**Poll adaptif** (`AUTO_POLL_FAST` / `AUTO_POLL`):
- **Cepat** (default 45s) saat ada akun **menunggu settlement lepas** → begitu EDELx unlock, segera submit.
- **Lambat** (default 180s) saat semua sudah submit / idle → hemat request.

```bash
AUTO_POLL_FAST=30 AUTO_POLL=200 python3 listing_bot.py --auto
```
Juga lewat CLI: menu **🔁 Auto Listing (tiap window)** (bisa atur interval cepat & lambat).

### 5. Kirim EDELx antar akun (bulk sender)
```bash
# fixed 110 ke beberapa akun:
python3 sender_bot.py --send --from a@.. --to b@..,c@.. --amount 110
# range 100–120 (acak) ke SEMUA akun lain:
python3 sender_bot.py --send --from a@..,b@.. --to-all --amount 100-120
MIN_WD=100 python3 sender_bot.py --send ...   # atur minimum (default 100)
```
- **Pilih 1+ sender**; nominal **fixed** (`110`) atau **range** (`100-120`, acak per transfer).
- **Auto rotasi**: kalau saldo sender < nominal, otomatis pindah ke sender lain; kalau semua kurang → berhenti.
- **Kebal**: `idempotencyKey` mencegah dobel-kirim saat retry; preapproval sender & penerima diaktifkan otomatis.
- Lewat CLI: menu **💸 Kirim EDELx (bulk)** (pilih sender via spasi, konfirmasi sebelum kirim).
- Endpoint: `POST /transfers` `{instrumentId, amount, toPartyId, idempotencyKey}`. Minimum **100 EDELx**.

### 6. CLI interaktif + dashboard
```bash
python3 edel_cli.py            # menu interaktif
python3 edel_cli.py dash       # langsung dashboard live
```
Menu: **Dashboard live · Jalankan Listing Calls · Kirim EDELx · Refresh Sesi · Status Fleet · Party IDs · Pantau Settlement**.

**Dashboard interaktif:** di tabel ACCOUNTS, navigasi dengan **↑/↓** (panah atas/bawah) untuk memilih
akun; baris terpilih di-highlight. Panel bawah menampilkan **detail + log akun terpilih** (EDELx
avail/staked/locked, round, sisa masa sesi, Party ID lengkap). Tekan **q** untuk keluar. Data akun
terpilih diperbarui otomatis. Butuh terminal sungguhan (TTY).

---

## Alur Listing Calls

Window berlangsung **per jam** (mis. 12:00–13:00 UTC).

1. `GET /portfolio` — cek saldo EDELx (`available`).
2. `POST /listing-round {}` — buka round. Server **mengunci seluruh EDELx available** sebagai stake,
   membuat **7 call** (head-to-head 2 saham), stake dibagi rata (mis. 110 ÷ 7 = 15.71 EDELx/call).
   Response berisi `preview.id` + daftar `options`.
3. **Pilih** 1 saham tiap call berdasarkan **Demand Index** (rank lebih tinggi = dipilih).
4. `POST /listing-round/submit {previewId, picks}` — kirim 7 pilihan. Status → `SUBMITTED`.
5. **Settlement** otomatis oleh server (~selectionCloses + ~8 menit). Stake kembali ke `available`.

Endpoint utama: `/portfolio`, `/demand-index`, `/listing-round` (GET & POST), `/listing-round/submit`,
`/airdrop/status`, `/airdrop/claim`.

---

## Sesi & keandalan

- **Login 1x, pakai berkali-kali**: cookie `edel_session` berlaku **~12 jam**. Selama valid, semua
  operasi HTTP tanpa login ulang.
- **Tidak ada refresh token** — saat cookie habis, bot **login ulang otomatis** (HTTP, tanpa browser).
- **signCount**: server memeriksa counter naik. Bot inject `signCount = waktu unix (detik)` agar selalu
  lolos.
- **Lapisan `api()` kebal terhadap:**
  - `401/403` (sesi mati) → login ulang otomatis lalu ulang request
  - `5xx` (server flaky) → retry dengan backoff eksponensial
  - error jaringan / proxy mati → retry + **rotasi ke proxy lain**
  - cookie baru otomatis disimpan ke `accounts.json`
- **Paralel aman**: `accounts.json` ditulis atomik (tmp+rename) di bawah **file-lock (fcntl)**, jadi
  banyak akun bisa jalan bersamaan tanpa korup data.

---

## Penanganan kondisi khusus

**Akun tidak ada / kredensial invalid**
Login gagal → akun di-`SKIP` dengan pesan `login gagal (akun tak terdaftar / server down)`. Bot tidak
crash, lanjut ke akun lain.

**Saldo EDELx kurang / kosong**
- `EDELx = 0` (belum deposit atau masih staked/locked) → `SKIP: EDELx=0`.
- Set batas minimum: `MIN_EDELX=5 python3 listing_bot.py --run` → akun dengan EDELx < 5 dilewati.
- Kalau server menolak buka round karena stake kurang → pesan `EDELx tak cukup untuk buka round
  (stake_lock_failed / listing_stake_holdings_not_found)`.

**Kondisi window / server lain (otomatis di-skip dengan pesan jelas):**

| Kode error | Arti |
|------------|------|
| `daily_round_limit` / `listing_round_limit` | Sudah kena batas window |
| `round_selection_not_open` | Window belum buka |
| `vault_stake_locker_disabled` / `vault_migration_freeze` | Listing calls sedang freeze |
| `previous_round_settlement_pending` | Settlement round sebelumnya belum selesai |

**Stake nyangkut (LockedPositionActive tak kunjung lepas)**
Settlement 100% di sisi server — **tidak ada endpoint unlock manual**. Biasanya lepas ~8 menit setelah
window tutup. Kalau jauh melewati `estimatedNextSettlementAttemptBy` masih terkunci → itu bug/keterlambatan
server Edel; hanya bisa menunggu retry server. Pantau lewat menu **Pantau Settlement**.

---

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| `ModuleNotFoundError: No module named 'requests'` | Dependency belum diinstall → `pip3 install -r requirements.txt` (lihat [Setup](#setup--instalasi-mesin-baru)) |
| `accounts.json` tak terbaca / 0 akun padahal sudah diisi | Dulu path hardcode; sekarang relatif ke script. Pastikan `accounts.json` ada di folder repo, atau set `EDEL_STATE=/path/accounts.json`. Cek: `python3 -c "import listing_bot as LB; print(LB.STATE, len(LB.load_accts()))"` |
| `rich`/`questionary` not found | `pip install rich questionary` |
| Register selalu gagal (`startStatus 5xx`) | Server Edel sering down; `run_until_100.sh` retry otomatis |
| Login 401 "Could not verify login" | `signCount` terlalu rendah (sudah di-handle: pakai unix detik) **atau** kunci privat salah (akun BAD) |
| Banyak `DOWN` saat verifikasi | Server flaky — jalankan `verify_accounts.py` lagi |
| Dashboard tampil terpotong | Butuh terminal sungguhan (bukan pipe); perbesar jendela |
| Akun tak bisa login setelah lama | Cookie expired → bot login ulang otomatis; kalau tetap gagal, cek proxy |

---

**Backup `accounts.json` sesering mungkin — itu satu-satunya kunci ke semua akun.**
