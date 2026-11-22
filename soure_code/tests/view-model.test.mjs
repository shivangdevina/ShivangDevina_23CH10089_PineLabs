import {test} from 'node:test';
import assert from 'node:assert/strict';
import {toPaise,percent,money,escapeHTML,currentWindow} from '../src/payments_analyst/static/view-model.js';
import {AuthClient} from '../src/payments_analyst/static/auth-client.js';

test('rupee inputs convert exactly to integer paise',()=>{
  assert.equal(toPaise('10.29'),1029);assert.equal(toPaise('0.01'),1);assert.equal(toPaise('1.'),100);
  assert.throws(()=>toPaise('-1'));assert.throws(()=>toPaise('1.001'));assert.throws(()=>toPaise('1e9'));assert.throws(()=>toPaise('10000001'));
});
test('missing observations are never displayed as zero',()=>{
  assert.equal(percent(null),'—');assert.equal(money(null),'—');assert.equal(percent(0),'0.0%');
});
test('user and provider text is escaped before HTML insertion',()=>{
  assert.equal(escapeHTML('<script>"x"</script>'),'&lt;script&gt;&quot;x&quot;&lt;/script&gt;');
});
test('exclusive snapshot boundaries display correct IST business dates',()=>{
  const result=currentWindow({current_start:'2026-11-15T00:00:00+05:30',window_end:'2026-11-22T00:00:00+05:30'});
  assert.match(result,/15 Nov/);assert.match(result,/21 Nov/);assert.match(result,/2026/);
});
test('late token responses cannot recreate a cleared session',async()=>{
  const original=globalThis.fetch;
  let resolve;
  globalThis.fetch=()=>new Promise(done=>{resolve=done;});
  const client=new AuthClient({supabase_url:'https://test.supabase.co',supabase_publishable_key:'sb_publishable_test'},()=>{});
  try{
    const login=client.signIn('test@example.com','synthetic-password');
    client.clear();
    resolve({ok:true,json:async()=>({access_token:'synthetic-token',refresh_token:'synthetic-refresh',expires_in:3600})});
    await assert.rejects(login,/Session changed/);
    assert.equal(client.session,null);
  }finally{client.clear();globalThis.fetch=original;}
});
