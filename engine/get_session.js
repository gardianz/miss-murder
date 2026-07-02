// get_session.js — login via passkey, output cookie edel_session (untuk reuse HTTP tanpa WebAuthn).
// args: email credentialJSON [proxyFull]
// signCount di-inject TINGGI (unix detik) karena server enforce counter (received > stored).
const p = require('puppeteer-extra');
const s = require('puppeteer-extra-plugin-stealth');
p.use(s());
const [,, email, credJson, proxyFull] = process.argv;
function parseProxy(u){ if(!u) return null; const m=u.match(/^https?:\/\/(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$/); return m?{user:m[1],pass:m[2],host:m[3],port:m[4]}:null; }
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

async function tryLogin(cred, px){
  const args=['--no-sandbox','--disable-setuid-sandbox','--disable-blink-features=AutomationControlled'];
  if(px) args.push('--proxy-server=http://'+px.host+':'+px.port);
  const b=await p.launch({headless:'new',executablePath:'/usr/bin/google-chrome-stable',args});
  const pg=await b.newPage();
  if(px&&px.user) await pg.authenticate({username:px.user,password:px.pass});
  const cdp=await pg.target().createCDPSession();
  await cdp.send('WebAuthn.enable');
  const {authenticatorId}=await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
  await cdp.send('WebAuthn.addCredential',{authenticatorId,credential:{
    credentialId:cred.credentialId, isResidentCredential:cred.isResidentCredential!==undefined?cred.isResidentCredential:true,
    rpId:cred.rpId, privateKey:cred.privateKey, userHandle:cred.userHandle,
    signCount: Math.floor(Date.now()/1000)  // tinggi & selalu naik -> lolos counter check
  }});
  let finish=null, start=null;
  pg.on('response',r=>{ if(r.url().includes('/auth/login/start'))start=r.status(); if(r.url().includes('/auth/login/finish'))finish=r.status(); });
  await pg.goto('https://runway.edel.finance/login',{waitUntil:'networkidle2',timeout:30000});
  await pg.waitForSelector('input#email',{timeout:15000});
  await pg.type('input#email', cred.email||email, {delay:50});
  await sleep(500);
  await pg.evaluate(()=>{document.querySelector('form')?.requestSubmit();});
  await sleep(12000);
  const okDesk=pg.url().includes('/desk');
  let session=null;
  if(okDesk){
    const cookies=await pg.cookies('https://runway.edel.finance');
    const c=cookies.find(x=>x.name==='edel_session');
    if(c) session={value:c.value, expires:c.expires, domain:c.domain, path:c.path};
  }
  await b.close();
  return {ok:okDesk, start, finish, session};
}

(async()=>{
  const cred=JSON.parse(credJson);
  const px=parseProxy(proxyFull);
  const MAX=parseInt(process.env.SESSION_MAX||'8',10);
  for(let i=0;i<MAX;i++){
    try{
      const r=await tryLogin(cred,px);
      if(r.ok && r.session){ console.log(JSON.stringify({ok:true, email:cred.email||email, session:r.session})); return; }
      if(r.finish===401){ console.log(JSON.stringify({ok:false, error:'login_401', ...r})); return; }
      process.stderr.write(`attempt ${i+1}: start=${r.start} finish=${r.finish} ok=${r.ok}\n`);
    }catch(e){ process.stderr.write(`attempt ${i+1} err: ${e.message}\n`); }
    await sleep(6000);
  }
  console.log(JSON.stringify({ok:false, error:'gagal setelah retry'}));
})();
