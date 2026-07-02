const p = require('puppeteer-extra');
const s = require('puppeteer-extra-plugin-stealth');
const crypto = require('crypto');
p.use(s());

// derive SPKI(base64) public key from an exported PKCS8 privateKey(base64). null on failure.
function pubFromPriv(pkB64){
  try {
    const key = crypto.createPrivateKey({ key: Buffer.from(pkB64,'base64'), format:'der', type:'pkcs8' });
    return crypto.createPublicKey(key).export({ format:'der', type:'spki' }).toString('base64');
  } catch(e){ return null; }
}

const [,, displayName, email, proxyFull] = process.argv;
function parseProxy(u){ if(!u) return null; const m=u.match(/^https?:\/\/(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$/); return m?{user:m[1],pass:m[2],host:m[3],port:m[4]}:null; }

(async () => {
  const px = parseProxy(proxyFull);
  const args = ['--no-sandbox','--disable-setuid-sandbox','--disable-blink-features=AutomationControlled'];
  if (px) args.push('--proxy-server=http://'+px.host+':'+px.port);
  
  const b = await p.launch({
    headless: 'new', executablePath: '/usr/bin/google-chrome-stable',
    args
  });
  const pg = await b.newPage();
  if (px && px.user) await pg.authenticate({ username: px.user, password: px.pass });

  const cdp = await pg.target().createCDPSession();
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: { protocol:'ctap2', transport:'internal', hasResidentKey:true, hasUserVerification:true, isUserVerified:true, automaticPresenceSimulation:true }
  });

  // record the credential the site actually registers (base64url id) so we export the RIGHT one.
  // server flakiness can trigger multiple credentials.create() calls -> multiple creds in authenticator.
  await pg.evaluateOnNewDocument(() => {
    window.__regCreds = [];
    const orig = navigator.credentials.create.bind(navigator.credentials);
    navigator.credentials.create = async (opts) => {
      const c = await orig(opts);
      try {
        let pub = null;
        if (c && c.response && typeof c.response.getPublicKey === 'function') {
          const pk = c.response.getPublicKey();
          if (pk) { const u = new Uint8Array(pk); let s=''; for (let i=0;i<u.length;i++) s+=String.fromCharCode(u[i]); pub = btoa(s); }
        }
        if (c && c.id) window.__regCreds.push({ id: c.id, pub });
      } catch(e){}
      return c;
    };
  });

  const b64url = s => (s||'').replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  const out = { email, displayName };
  pg.on('response', async r => {
    const u = r.url();
    if (u.includes('/auth/register/start') && r.request().method()==='POST') out.startStatus = r.status();
    if (u.includes('/auth/register/finish')) {
      r.text().then(t => { try { out.profile = JSON.parse(t).profile; } catch(e){} }).catch(()=>{});
    }
  });

  await pg.goto('https://runway.edel.finance/register', { waitUntil:'networkidle2', timeout:45000 });
  await pg.waitForSelector('input#email', { timeout:20000 });

  await pg.type('input#displayName', displayName, { delay:50 });
  await pg.type('input#email', email, { delay:50 });
  
  // click checkbox
  await pg.evaluate(() => { const c=document.querySelector('#legal-acceptance'); if(c&&!c.checked) c.click(); });
  await new Promise(r=>setTimeout(r,800));

  // submit
  await pg.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button[type="submit"]')).find(x=>x.innerText.includes('Create'));
    const form = btn ? btn.closest('form') : document.querySelector('form');
    if(form) form.requestSubmit(btn||undefined); else if(btn) btn.click();
  });

  // poll for /desk — server slow, register/finish can take a while
  for (let i=0; i<20 && !pg.url().includes('/desk'); i++) await new Promise(r=>setTimeout(r,2000));
  out.finalUrl = pg.url();
  out.ok = out.finalUrl.includes('/desk');

  // get creds — export the credential the SITE actually registered, not blindly creds[0].
  // multiple credentials.create() (server flakiness / SPA retry) leaves stale creds whose pubkey
  // the server never stored -> those fail login with "Could not verify login".
  if (out.ok) {
    try {
      const regCreds = await pg.evaluate(() => window.__regCreds || []);
      const allCreds = await cdp.send('WebAuthn.getCredentials', { authenticatorId });
      const creds = allCreds.credentials || [];
      out.credCount = creds.length;
      out.regCount = regCreds.length;

      // pick the exported credential whose private key ACTUALLY matches the public key
      // that was registered with the server (verify locally, no server needed).
      // prefer the most-recently registered credential.
      let raw = null, verified = false;
      for (let i = regCreds.length - 1; i >= 0 && !raw; i--) {
        const rc = regCreds[i];
        const cand = creds.find(c => b64url(c.credentialId || c.id) === b64url(rc.id));
        if (!cand) continue;
        if (rc.pub) {
          const der = pubFromPriv(cand.privateKey);
          if (der && der === rc.pub) { raw = cand; verified = true; }  // cryptographically confirmed
        } else if (!raw) {
          raw = cand;  // getPublicKey unavailable -> match by id only
        }
      }
      // fallbacks if nothing matched (e.g. no regCreds captured)
      if (!raw && creds.length) raw = creds.slice().sort((a,b)=>(b.signCount||0)-(a.signCount||0))[0];
      if (!raw && creds.length) raw = creds[creds.length-1];

      // credVerified: true=key proven correct, false=had pubkeys but NONE matched (bad export),
      // null=could not verify (getPublicKey unsupported)
      out.credVerified = verified ? true : (regCreds.some(r => r.pub) ? false : null);

      if (raw) {
        const rc = regCreds.find(x => b64url(x.id) === b64url(raw.credentialId || raw.id));
        out.credential = {
          credentialId: raw.credentialId || raw.id,
          isResidentCredential: raw.isResidentCredential !== undefined ? raw.isResidentCredential : true,
          privateKey: raw.privateKey || '',
          publicKey: (rc && rc.pub) || pubFromPriv(raw.privateKey) || '',  // SPKI base64 (untuk verifikasi offline; login tak butuh)
          userHandle: raw.userHandle || '',
          rpId: raw.rpId || 'edel.finance',
          signCount: raw.signCount || 1
        };
      }
    } catch(e) { out.credErr = e.message; }
  }

  console.log(JSON.stringify(out));
  await b.close();
})().catch(e => { console.error('ERR:', e.message); process.exit(1); });