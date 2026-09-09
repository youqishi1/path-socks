const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(__dirname+'/client-helper.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const sandbox={module:{exports:{}},TextEncoder,URLSearchParams};
vm.runInNewContext(code,sandbox);
const helper=sandbox.module.exports;
if(process.argv[2]==='export') {
  const {c,r}=JSON.parse(fs.readFileSync(0,'utf8'));
  process.stdout.write(JSON.stringify({xray:helper.xrayConfig(c,r),mihomo:helper.mihomoConfig(c,r)}));
} else {
  const c={host:'8.8.8.8',port:26443,id:'00000000-0000-0000-0000-000000000001',sni:'www.microsoft.com',public:'A'.repeat(43),sid:'0123456789abcdef'};
  const r={host:'9.9.9.9',port:1080,user:'a:b@x',pass:'secret:#\"'};
  const x=helper.xrayConfig(c,r),m=helper.mihomoConfig(c,r);
  assert.equal(x.outbounds[0].tag,'residential');
  assert.equal(x.outbounds[0].proxySettings.tag,'vps');
  assert.equal(x.outbounds[0].settings.servers[0].users[0].pass,r.pass);
  assert(!x.outbounds.some(p=>p.protocol==='freedom'));
  assert.equal(m.proxies[1]['dialer-proxy'],'VPS');
  assert.equal(m['proxy-groups'][0].proxies.length,1);
  assert.equal(m['proxy-groups'][0].proxies[0],'RESIDENTIAL');
  assert(!JSON.stringify(m).includes('DIRECT'));
  assert.throws(()=>helper.xrayConfig(c,{...r,host:c.host}));
  assert.throws(()=>helper.xrayConfig(c,{...r,port:65536}));
  assert(helper.relayURI(c).includes('security=reality'));
  console.log('PASS: local helper, credentials escaping, chain direction and no direct fallback');
}
