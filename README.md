# Edel Runway Desk — Bot Otomatisasi

Bot untuk **register akun massal**, **login**, **auto-tangkap access code dari Telegram**, dan
**otomatisasi Listing Calls** di [runway.edel.finance](https://runway.edel.finance) — semuanya
**tanpa browser** (full HTTP + passkey WebAuthn virtual).

Semua otentikasi berbasis **passkey Ed25519** (tidak ada password / verifikasi email). Kunci privat
tiap akun disimpan lokal di `accounts.json` dan dipakai untuk login berulang selamanya.

> ⚠️ **PENTING — tidak ada recovery.** Kalau `privateKey` sebuah akun hilang, akun itu **tak bisa
> diakses selamanya** (tak ada reset email / tambah passkey). **Backup `accounts.json` berkala.**

---

## Daftar Isi
1. [Fitur utama](#fitur-utama)
2. [Prasyarat](#prasyarat)
3. [Instalasi (mesin baru)](#instalasi-mesin-baru)
4. [Konfigurasi (.env)](#konfigurasi-env)
5. [Cara menjalankan](#cara-menjalankan)
6. [Alur Listing Calls](#alur-listing-calls)
7. [Data akun (accounts.json)](#data-akun)
8. [Sesi & keandalan](#sesi--keandalan)
9. [Penanganan kondisi khusus](#penanganan-kondisi-khusus)
10. [Troubleshooting](#troubleshooting)
11. [Keamanan](#keamanan)

---

## Fitur utama

- **Register full-HTTP** — bikin keypair Ed25519 sendiri + attestation WebAuthn `none`, kirim ke
  server. Tanpa Chrome/puppeteer. Akun langsung `credVerified=true`.
- **Register sekarang butuh access code** (dibagikan dev di channel Telegram). Kode **sekali pakai**
  → siapa cepat dia dapat (FCFS race).
- **Watcher Telegram** — pantau beberapa channel sekaligus, **auto-tangkap access code** begitu
  di-share, langsung register. Dioptimasi untuk balapan (prewarm koneksi + identitas).
- **Login tanpa browser** — bot bikin WebAuthn assertion sendiri (tanda tangan Ed25519) → cookie sesi.
- **Listing Calls** — pilih 1 dari 2 saham per "call" berdasarkan Demand Index, lalu submit. Bisa
  otomatis tiap window.
- **Bulk sender EDELx** — kirim token antar akun (rotasi sender, idempotent).
- **Pre-approval receiving semua token** — aktifkan penerimaan EDELx + **CC (Canton Coin, reward)**.
  CC default OFF; tanpa ini reward CC tak masuk.
- **Dashboard live** (gaya Bloomberg) — saldo EDELx & CC per akun, alert Telegram saat reward CC masuk.

---

## Prasyarat

- **Python 3.9+** (`python3 --version`)
- **Git**
- (opsional) **proxy** — untuk sebar IP saat menjalankan banyak akun. Bot jalan tanpa proxy juga.
- Untuk **Watcher Telegram**: akun Telegram + **API ID & API HASH** dari <https://my.telegram.org>.

---

## Instalasi (mesin baru)

```bash
# 1. Clone repo (privat)
git clone https://github.com/gardianz/miss-murder.git
cd miss-murder

# 2. Install dependency Python
pip3 install -r requirements.txt
#   Kalau kena "externally-managed-environment" (Ubuntu baru):
pip3 install --break-system-packages -r requirements.txt
#   ATAU pakai virtualenv (paling bersih):
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 3. Siapkan file konfigurasi .env
cp .env.example .env
nano .env            # isi minimal: EDEL_TG_API_ID, EDEL_TG_API_HASH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 4. Siapkan data akun — accounts.json TIDAK ikut repo (berisi private key, gitignore)
#    a) SALIN dari mesin lama:   scp user@vps-lama:~/edel/miss-murder/accounts.json .
#    b) atau mulai dari nol:     kosong dulu, nanti diisi lewat register/watcher

# 5. (opsional) proxy
cp .proxies.txt.example ~/.proxies.txt   # lalu isi proxy asli (1 per baris), atau lewati kalau tanpa proxy

# 6. Jalankan
python3 edel_cli.py
```

Dependency (`requirements.txt`): `requests`, `cryptography`, `rich`, `questionary`, `telethon`.

**Catatan:**
- File rahasia **tidak ikut repo** (gitignore) — wajib disiapkan sendiri: `accounts.json`, `.env`,
  `~/.proxies.txt`, `*.session` (Telegram), `party_ids.csv`.
- Path `accounts.json` default = folder repo. Override: env `EDEL_STATE=/path/ke/accounts.json`.

---

## Konfigurasi (.env)

Bot otomatis membaca `.env` (format `KEY=VALUE`, tanpa spasi di sekitar `=`). Salin dari
`.env.example`. Kunci penting:

| Kunci | Wajib | Fungsi |
|-------|-------|--------|
| `TELEGRAM_BOT_TOKEN` | untuk notif | Token bot dari [@BotFather](https://t.me/BotFather) — kirim alert (submit, reward CC) |
| `TELEGRAM_CHAT_ID` | untuk notif | Chat id tujuan alert (dari [@userinfobot](https://t.me/userinfobot)) |
| `EDEL_TG_API_ID` | untuk watcher | API ID Telegram dari <https://my.telegram.org> |
| `EDEL_TG_API_HASH` | untuk watcher | API HASH Telegram |
| `EDEL_TG_WATCH` | untuk watcher | Channel dipantau, pisah koma. Contoh: `handlpay,oneswap_community,edeldotfinance` |
| `EDEL_TG_POLL` | – | Detik antar cek pesan baru (default `1.0`; `0.5` lebih agresif; `0`=off) |
| `EDEL_PREWARM` | – | Jumlah identitas + koneksi disiapkan sebelum kode drop (klaim instan). Default `16` |
| `EDEL_AUTO_RECV_CC` | – | `1` (default) = akun baru auto-enable receiving CC biar reward bisa masuk |
| `REG_ACCESS_CODE` | – | Isi manual kalau mau register tanpa watcher (1 kode) |
| `NO_PROXY` | – | `1` = paksa koneksi langsung (abaikan proxy) |
| `WORKERS` | – | Jumlah akun paralel untuk listing calls (4–6 disarankan) |

> Pertama kali menjalankan watcher, Telegram minta **nomor HP + OTP** sekali (login MTProto).
> Sesi tersimpan di `watch.session` (rahasia, jangan di-commit).

---

## Cara menjalankan

Cara termudah: **CLI interaktif**. Semua fitur ada di menu.

```bash
python3 edel_cli.py
```

Menu:

| Menu | Fungsi |
|------|--------|
| 📊 Dashboard live | Pantau pasar + saldo EDELx & CC semua akun, real-time |
| ➕ Register Akun (HTTP) | Register akun baru (butuh access code) |
| 👀 Watcher Kode Telegram | Auto-tangkap access code dari channel → langsung register |
| 🍪 Import Akun (cookie ekstensi) | Impor akun login-manual dari Chrome extension |
| ▶ Jalankan Listing Calls (sekali) | Listing calls semua akun, 1 putaran |
| 🔁 Auto Listing (tiap window) | Loop otomatis, submit tiap window |
| 💸 Kirim EDELx (bulk) | Transfer EDELx antar akun |
| 🔓 Pre-Approve Semua Token | Aktifkan receiving EDELx + CC (biar reward CC masuk) |
| 🔑 Refresh Sesi | Login ulang akun sesi mati |
| 📈 Riwayat Listing Call | Statistik submit per akun |
| 📋 Status Fleet · 🏦 Party IDs · ⏳ Pantau Settlement | Info & deposit & pantau settlement |
| 🌐 Proxy: ON/OFF | Toggle pakai proxy atau koneksi langsung |

Tiap fitur juga bisa dijalankan langsung dari terminal (berguna untuk daemon / screen):

### 1. Watcher Telegram (auto-register) — fitur andalan

Pantau channel, tangkap access code otomatis, langsung register akun.

```bash
python3 edel_watch.py                     # jalan pakai channel di EDEL_TG_WATCH
python3 edel_watch.py --catchup           # scan pesan lama dulu saat start
python3 edel_watch.py --test "KODE-DI-SINI"   # tes apakah format kode terbaca (tanpa register)
python3 edel_watch.py --code "KODE"       # register 1 kode manual
```

- Karena kode **sekali pakai + FCFS**, watcher menyiapkan koneksi TLS & identitas (`EDEL_PREWARM`) di
  awal supaya klaim secepat mungkin.
- Akun baru yang berhasil dibuat **otomatis di-enable receiving CC** (`EDEL_AUTO_RECV_CC=1`) →
  reward CC bisa langsung masuk.
- Notif tiap batch (kode masuk, akun jadi) dikirim ke Telegram alert.
- Jalan permanen di background:
  ```bash
  screen -dmS edelwatch python3 -u edel_watch.py > watch.log 2>&1
  screen -r edelwatch          # lihat (Ctrl-A D untuk keluar tanpa stop)
  ```

### 2. Register manual (tanpa watcher)

Kalau sudah pegang access code sendiri:

```bash
REG_ACCESS_CODE=KODE-KAMU python3 register_http.py 1      # register 1 akun pakai kode itu
REG_WORKERS=8 REG_ACCESS_CODE=KODE python3 register_http.py 5
```

Register bikin keypair Ed25519 sendiri + attestation `none` (server dukung alg -8). Akun langsung
`credVerified=true`. Tanpa Chrome. **Tanpa access code valid → gagal** (`REGISTRATION_ACCESS_CODE_REQUIRED`).

### 3. Listing Calls

```bash
python3 listing_bot.py --run                 # SEKALI, semua akun (paralel)
python3 listing_bot.py --run <email>         # 1 akun
WORKERS=6 python3 listing_bot.py --run       # atur paralel (4–6 disarankan)
MIN_EDELX=5 python3 listing_bot.py --run     # skip akun EDELx < 5
```

**Auto (otomatis tiap window):**
```bash
python3 listing_bot.py --auto
# permanen di background:
screen -dmS edelauto bash -c 'WORKERS=6 AUTO_POLL_FAST=25 python3 -u listing_bot.py --auto > auto.log 2>&1'
```
Mode auto submit semua akun ber-EDELx yang belum submit. Aman dipanggil berulang (akun `SUBMITTED`
atau EDELx=0 dilewati). **Poll adaptif**: cepat saat menunggu settlement lepas, lambat saat idle.

### 4. Pre-approve receiving (penting untuk reward CC)

Reward listing call dibayar dalam **CC (Canton Coin)**. Receiving CC **default OFF** di tiap akun —
tanpa diaktifkan, reward tak masuk. Aktifkan sekali untuk semua akun:

```
Menu CLI → 🔓 Pre-Approve Semua Token → Semua akun
```
Fitur ini login tiap akun + aktifkan receiving EDELx & CC, dengan **re-sweep otomatis** (retry akun
yang gagal beberapa ronde) karena aktivasi CC adalah operasi on-chain yang kadang butuh diulang.
Akun baru dari watcher sudah auto-enable, jadi ini terutama untuk akun lama.

### 5. Kirim EDELx antar akun

```bash
python3 sender_bot.py --send --from a@.. --to b@..,c@.. --amount 110
python3 sender_bot.py --send --from a@..,b@.. --to-all --amount 100-120
```
Pilih 1+ sender, nominal fixed/range, auto-rotasi sender saat saldo kurang, idempotent (anti
dobel-kirim). Minimum 100 EDELx. Lebih mudah lewat menu **💸 Kirim EDELx (bulk)**.

### 6. Dashboard

```bash
python3 edel_cli.py dash
```
Header (waktu lokal + server), MARKET/WINDOW + countdown, DEMAND INDEX, ringkasan fleet, tabel
ACCOUNTS (EDELx + **CC** per akun). Navigasi **↑/↓**, **q** keluar. Alert Telegram `🎁 CC reward
masuk` otomatis saat saldo CC sebuah akun naik. Butuh terminal sungguhan (TTY).

---

## Alur Listing Calls

Window berlangsung **6 jam** (mis. 00:00–06:00 UTC).

1. `GET /portfolio` — cek saldo EDELx (`available`).
2. `POST /listing-round {}` — buka round. Server **mengunci seluruh EDELx available** sebagai stake,
   membuat beberapa **call** (head-to-head 2 saham), stake dibagi rata. Response: `preview.id` + `options`.
3. **Pilih** 1 saham tiap call berdasarkan **Demand Index** (rank lebih tinggi = dipilih).
4. `POST /listing-round/submit {previewId, picks}` — kirim pilihan. Status → `SUBMITTED`.
5. **Settlement** otomatis server (~selectionCloses + ~8 menit). Stake kembali ke `available`.
   Reward (kalau menang) dibayar dalam **CC** — butuh receiving CC aktif (lihat pre-approve).

> Kalah listing call **tidak** menghilangkan EDELx — stake kembali setelah settlement.

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
    "publicKey": "...",
    "userHandle": "...",
    "rpId": "edel.finance",
    "signCount": 1
  },
  "session": { "value": "<cookie edel_session>", "expires": 1783000000 }
}
```

- **Party ID** (`hostedPartyId`) = alamat deposit EDELx.
- `credVerified: true` → aman dipakai. `false` → kunci salah (register ulang).

---

## Sesi & keandalan

- **Login 1x, pakai berkali-kali**: cookie `edel_session` berlaku **~12 jam**.
- **Tidak ada refresh token** — saat cookie habis, bot **login ulang otomatis** (HTTP, tanpa browser).
- **signCount**: server memeriksa counter naik; bot inject `signCount = waktu unix (detik)` agar lolos.
- **Lapisan `api()` kebal terhadap:**
  - `401/403` (sesi mati) → login ulang otomatis lalu ulang request
  - `5xx` (server flaky) → retry backoff eksponensial
  - `429` (rate-limit nginx saat banyak akun 1 IP) → backoff + jitter + rotasi proxy
  - error jaringan / proxy mati → retry + rotasi proxy lain
  - cookie baru otomatis disimpan ke `accounts.json`
- **Paralel aman**: `accounts.json` ditulis atomik (tmp+rename) di bawah **file-lock (fcntl)**.

---

## Penanganan kondisi khusus

**Register**
- `REGISTRATION_ACCESS_CODE_REQUIRED` — belum kasih access code.
- `REGISTRATION_ACCESS_CODE_INVALID` — kode salah / **sudah kepakai** (kalah balapan FCFS).
- `USER_ALREADY_REGISTERED` — email sudah dipakai (jarang; bot generate email unik).

**Saldo EDELx kurang / kosong** — `EDELx=0` (belum deposit atau masih staked/locked) → di-skip.
Set `MIN_EDELX=5` untuk lewati akun EDELx < 5.

**Kondisi window / server (otomatis di-skip dengan pesan jelas):**

| Kode error | Arti |
|------------|------|
| `daily_round_limit` / `listing_round_limit` | Sudah kena batas window |
| `round_selection_not_open` | Window belum buka |
| `vault_stake_locker_disabled` / `vault_migration_freeze` | Listing calls sedang freeze |
| `previous_round_settlement_pending` | Settlement round sebelumnya belum selesai |

**Stake nyangkut** — settlement 100% sisi server, tak ada unlock manual. Biasanya lepas ~8 menit
setelah window tutup. Pantau lewat menu **Pantau Settlement**.

---

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| `ModuleNotFoundError` (`requests`/`telethon`/…) | `pip3 install -r requirements.txt` (lihat [Instalasi](#instalasi-mesin-baru)) |
| Watcher: `EDEL_TG_API_ID / EDEL_TG_API_HASH belum di-set` | Isi keduanya di `.env` (dari <https://my.telegram.org>) |
| Watcher tidak menangkap kode | Cek `--test "KODE"` apakah format terbaca; pastikan channel benar di `EDEL_TG_WATCH` & akun anggota channel |
| Register selalu `ACCESS_CODE_INVALID` | Kode sekali-pakai sudah dikonsumsi orang lain (FCFS) — pakai watcher biar lebih cepat |
| Reward CC tak masuk | Receiving CC belum aktif → jalankan **🔓 Pre-Approve Semua Token** |
| `accounts.json` 0 akun padahal terisi | Pastikan file di folder repo, atau set `EDEL_STATE=/path/accounts.json` |
| Login 401 "Could not verify login" | Kunci privat salah (akun BAD) atau signCount (sudah di-handle) |
| Banyak `429` saat listing massal | Pakai proxy (sebar IP) atau turunkan `WORKERS` |
| Dashboard tampil terpotong | Butuh terminal sungguhan (bukan pipe); perbesar jendela |

---

## Keamanan

- **`accounts.json` = semua kunci privat akun** (akses aset EDELx/CC nyata). **JANGAN commit / share.**
- File rahasia (gitignore): `accounts.json`, `.env`, `~/.proxies.txt`, `*.session`, `party_ids.csv`.
- Repo GitHub **wajib privat**.
- **Backup `accounts.json` sesering mungkin — itu satu-satunya kunci ke semua akun.**
