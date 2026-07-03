const DOMAIN = "runway.edel.finance";
const BASE = "https://" + DOMAIN;

const $ = (id) => document.getElementById(id);
function setStatus(msg, cls) { const s = $("status"); s.textContent = msg; s.className = cls || ""; }

async function getCookie() {
  // chrome.cookies BISA baca cookie httpOnly (document.cookie tidak bisa)
  return await chrome.cookies.get({ url: BASE, name: "edel_session" });
}

async function getProfile() {
  try {
    // host_permissions -> cookie ikut terkirim (credentials:'include')
    const r = await fetch(BASE + "/profile", { credentials: "include", headers: { "Accept": "application/json" } });
    if (r.ok) return await r.json();
  } catch (e) {}
  return null;
}

async function run() {
  setStatus("Mengambil cookie...", "");
  $("out").value = "";
  const c = await getCookie();
  if (!c || !c.value) {
    setStatus("❌ Tidak ada cookie edel_session. Login dulu di " + DOMAIN, "err");
    return;
  }
  const prof = await getProfile();
  const p = (prof && prof.profile) || {};
  // expirationDate = detik (float) sejak epoch; kalau kosong (session cookie) pakai +12 jam
  const expires = c.expirationDate ? Math.floor(c.expirationDate) : Math.floor(Date.now() / 1000) + 12 * 3600;
  const acct = {
    email: p.email || "ISI_EMAIL_KAMU@gmail.com",
    displayName: p.displayName || null,
    ok: true,
    manual: true,
    proxy: null,
    profileId: p.id || null,
    hostedPartyId: p.hostedPartyId || null,
    transferPreapprovalContractId: p.transferPreapprovalContractId || null,
    session: { value: c.value, expires: expires }
  };
  $("out").value = JSON.stringify(acct, null, 2);
  const mins = Math.round((expires - Date.now() / 1000) / 60);
  if (p.email) setStatus(`✅ ${p.email} — cookie valid ~${mins} menit`, "ok");
  else setStatus(`⚠️ Cookie OK tapi /profile gagal — isi email manual. Valid ~${mins} menit`, "err");
}

async function copyOut() {
  const v = $("out").value.trim();
  if (!v) return;
  try { await navigator.clipboard.writeText(v); setStatus("📋 JSON disalin ke clipboard", "ok"); }
  catch (e) { $("out").select(); document.execCommand("copy"); setStatus("📋 Disalin", "ok"); }
}

$("btn").addEventListener("click", run);
$("copy").addEventListener("click", copyOut);
