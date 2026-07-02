// edel_login.js — login via saved passkey credential, retry on 5xx
// args: email credentialJSON [proxyFull]
// credential.signCount MUST be the exported signCount (not 0!) — server checks received > stored
const p = require('puppeteer-extra');
const s = require('puppeteer-extra-plugin-stealth');
p.use(s());

const [,, email, credJson, proxyFull] = process.argv;
function parseProxy(u){ if(!u) return null; const m=u.match(/^https?:\/\/(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$/); return m?{user:m[1],pass:m[2],host:m[3],port:m[4]}:null; }
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }

async function tryLogin(cred, email, px) {
  const args = ['--no-sandbox','--disable-setuid-sandbox','--disable-blink-features=AutomationControlled'];
  if (px) args.push('--proxy-server=http://'+px.host+':'+px.port);
  const b = await p.launch({ headless:'new', executablePath:'/usr/bin/google-chrome-stable', args });
  const pg = await b.newPage();
  if (px && px.user) await pg.authenticate({ username: px.user, password: px.pass });

  const cdp = await pg.target().createCDPSession();
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: { protocol:'ctap2', transport:'internal', hasResidentKey:true, hasUserVerification:true, isUserVerified:true, automaticPresenceSimulation:true }
  });

  // signCount must equal exported value — authenticator increments to signCount+1
  // server checks: received_count(signCount+1) > stored_count(signCount) = TRUE
  await cdp.send('WebAuthn.addCredential', { authenticatorId, credential: {
    credentialId: cred.credentialId,
    isResidentCredential: cred.isResidentCredential,
    rpId: cred.rpId,
    privateKey: cred.privateKey,
    userHandle: cred.userHandle,
    signCount: cred.signCount  // use exported value, NOT 0
  }});

  const out = { email, startStatus: null, finishStatus: null };
  pg.on('response', async r => {
    if (r.url().includes('/auth/login/start') && r.request().method()==='POST')
      out.startStatus = r.status();
    if (r.url().includes('/auth/login/finish')) {
      out.finishStatus = r.status();
      if (r.status() === 200) {
        try { out.profile = (await r.json()).profile; } catch(e){}
      } else {
        try { out.finishBody = (await r.text()).slice(0,300); } catch(e){}
      }
    }
  });

  await pg.goto('https://runway.edel.finance/login', { waitUntil:'networkidle2', timeout:30000 });
  await pg.waitForSelector('input#email', { timeout:15000 });
  await pg.type('input#email', email, { delay:55 });
  await sleep(500);
  await pg.evaluate(() => { document.querySelector('form')?.requestSubmit(); });
  await sleep(15000);

  out.finalUrl = pg.url();
  out.ok = out.finalUrl.includes('/desk');
  await b.close();
  return out;
}

(async () => {
  const cred = JSON.parse(credJson);
  const px = parseProxy(proxyFull);
  const MAX = parseInt(process.env.LOGIN_MAX_ATTEMPTS || '3', 10);
  let attempt = 0, last = null;
  while (attempt < MAX) {
    attempt++;
    let res;
    try {
      res = await tryLogin(cred, email, px);
    } catch (e) {
      process.stderr.write(`attempt #${attempt} threw: ${e.message}\n`);
      last = { ok:false, error:'exception', detail:e.message };
      await sleep(Math.min(8 * attempt, 30) * 1000);
      continue;
    }
    last = res;
    if (res.ok) { console.log(JSON.stringify(res)); return; }
    if (res.finishStatus === 401) {
      // credential invalid — signCount mismatch or key rejected
      res.error = 'credential_invalid_401';
      console.log(JSON.stringify(res));
      return;
    }
    const wait = Math.min(8 * attempt, 30);
    process.stderr.write(`retry #${attempt}/${MAX} in ${wait}s (start=${res.startStatus} finish=${res.finishStatus})\n`);
    if (attempt < MAX) await sleep(wait * 1000);
  }
  // exhausted — emit last result so caller gets JSON, not a timeout
  console.log(JSON.stringify({ ...(last||{}), ok:false, error: (last&&last.error) || 'max_attempts_exhausted' }));
})().catch(e => { console.error('ERR:', e.message); process.exit(1); });
