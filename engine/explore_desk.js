// explore_desk.js — login via passkey (proxy), lalu dump struktur /desk + semua API calls.
// args: email credentialJSON [proxyFull]
const p = require('puppeteer-extra');
const s = require('puppeteer-extra-plugin-stealth');
p.use(s());
const [,, email, credJson, proxyFull] = process.argv;
function parseProxy(u){ if(!u) return null; const m=u.match(/^https?:\/\/(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$/); return m?{user:m[1],pass:m[2],host:m[3],port:m[4]}:null; }
const sleep = ms => new Promise(r=>setTimeout(r,ms));

async function tryLogin(cred, px){
  const args=['--no-sandbox','--disable-setuid-sandbox','--disable-blink-features=AutomationControlled'];
  if(px) args.push('--proxy-server=http://'+px.host+':'+px.port);
  const b=await p.launch({headless:'new',executablePath:'/usr/bin/google-chrome-stable',args});
  const pg=await b.newPage();
  if(px&&px.user) await pg.authenticate({username:px.user,password:px.pass});
  const cdp=await pg.target().createCDPSession();
  await cdp.send('WebAuthn.enable');
  const {authenticatorId}=await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
  await cdp.send('WebAuthn.addCredential',{authenticatorId,credential:{credentialId:cred.credentialId,isResidentCredential:cred.isResidentCredential!==undefined?cred.isResidentCredential:true,rpId:cred.rpId,privateKey:cred.privateKey,userHandle:cred.userHandle,signCount:cred.signCount}});

  const api=[];
  pg.on('response',async r=>{
    const u=r.url();
    if(u.includes('/auth/')||u.includes('/desk')) return; // skip noise
    const host=u.replace(/^https?:\/\//,'').split('/')[0];
    if(!/edel\.finance|weling/.test(host)) return;
    const ct=(r.headers()['content-type']||'');
    if(ct.includes('json')){
      let body=''; try{body=(await r.text()).slice(0,1200)}catch(e){}
      api.push({url:u.replace('https://runway.edel.finance',''),status:r.status(),body});
    }
  });

  let finish=null, start=null;
  pg.on('response',r=>{ if(r.url().includes('/auth/login/start')) start=r.status(); if(r.url().includes('/auth/login/finish')) finish=r.status(); });
  await pg.goto('https://runway.edel.finance/login',{waitUntil:'networkidle2',timeout:30000});
  await pg.waitForSelector('input#email',{timeout:15000});
  await pg.type('input#email',cred.email||email,{delay:50});
  await sleep(500);
  await pg.evaluate(()=>{document.querySelector('form')?.requestSubmit();});
  await sleep(12000);
  const url=pg.url();
  const ok=url.includes('/desk');
  let dump=null;
  if(ok){
    // eksplor DOM /desk
    dump=await pg.evaluate(()=>({
      title:document.title, url:location.href,
      links:[...document.querySelectorAll('a')].map(a=>({t:(a.innerText||'').trim().slice(0,40),href:a.getAttribute('href')})).filter(x=>x.t||x.href),
      buttons:[...new Set([...document.querySelectorAll('button')].map(b=>(b.innerText||'').trim().slice(0,50)).filter(Boolean))],
      bodyText:(document.body.innerText||'').slice(0,3000),
    }));
    // coba klik/temukan link listing calls
    await sleep(1000);
  }
  const out={email:cred.email||email, start, finish, url, ok, dump, api};
  await b.close();
  return out;
}

(async()=>{
  const cred=JSON.parse(credJson);
  const px=parseProxy(proxyFull);
  const MAX=parseInt(process.env.EXPLORE_MAX||'6',10);
  for(let i=0;i<MAX;i++){
    try{
      const r=await tryLogin(cred,px);
      if(r.ok){ console.log(JSON.stringify(r)); return; }
      if(r.finish===401){ console.log(JSON.stringify(r)); return; }  // credential/signCount reject — jangan retry
      process.stderr.write(`attempt ${i+1}: start=${r.start} finish=${r.finish} url=${r.url}\n`);
    }catch(e){ process.stderr.write(`attempt ${i+1} err: ${e.message}\n`); }
    await sleep(6000);
  }
  console.log(JSON.stringify({ok:false,error:'login gagal setelah retry'}));
})();
