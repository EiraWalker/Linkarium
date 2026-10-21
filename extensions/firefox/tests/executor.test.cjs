// External browser storage and HTTP faults complement the real Firefox acceptance.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {webcrypto} = require('node:crypto');
const tick = () => new Promise(resolve => setImmediate(resolve));
const config = (endpoint = 'http://127.0.0.1:1234/v1', apiKey = 'fixture-key') => ({endpoint, apiKey, model: 'fixture', mode: 'json_object', verified: false});

function environment(values, fetcher, cloud = async () => ({ok: true, data: null}), failWrite = () => false) {
  const listeners = [];
  const browser = {
    storage: {local: {
      async get(keys) {return Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(key => [key, structuredClone(values[key])]))},
      async set(update) {if (failWrite(update)) throw new Error('fixture quota'); Object.assign(values, structuredClone(update));},
      async remove(key) {delete values[key];},
    }},
    permissions: {contains: async () => true},
    alarms: {onAlarm: {addListener() {}}, create() {}},
    runtime: {id: 'fixture', getURL: file => 'moz-extension://fixture/' + file,
      onMessage: {addListener: listener => listeners.push(listener)}},
  };
  const context = vm.createContext({browser, LocalCloud: {request: cloud}, Linkarium: {ORIGIN: 'https://link.eirawav.me'},
    crypto: webcrypto, URL, AbortSignal, TextEncoder, TextDecoder, setTimeout: () => 0,
    setInterval: () => 0, clearInterval() {}, fetch: fetcher});
  for (const name of ['model', 'executor']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/', name + '.js'), 'utf8'), context);
  return {context, values, send: message => listeners[0](message, {id: 'fixture', url: 'moz-extension://fixture/status.html'})};
}
function reply(content = '{"ok":true}') {
  return new Response(JSON.stringify({choices: [{message: {content}, finish_reason: 'stop'}]}));
}

test('background binds retained credentials to the actual stored origin across stale settings tabs', async () => {
  let calls = 0;
  const env = environment({localAIConfig: config('https://provider-b.example/v1', 'fixture-b-key')}, async () => {calls++; return reply();});
  await tick();
  const rejected = await env.send({kind: 'local-ai-validate', config: {...config('https://provider-a.example/v1'), apiKey: undefined}});
  assert.equal(rejected.ok, false);
  assert.equal(calls, 0);
  assert.equal(env.values.localAIConfig.apiKey, 'fixture-b-key');
  const valid = await env.send({kind: 'local-ai-validate', config: {...config('https://provider-b.example/v1'), apiKey: undefined}});
  assert.equal(valid.ok, true);
  assert.equal(calls, 1);
  assert.equal(env.values.localAIConfig.apiKey, 'fixture-b-key');
});

test('an older cloud protocol gives an explicit upgrade message before submitting a bookmark', async () => {
  let calls = 0;
  const browser = {storage: {local: {get: async () => ({}), set: async () => {}}}, tabs: {
    query: async () => [{id: 1, url: 'https://link.eirawav.me/', incognito: false}],
    sendMessage: async () => {calls++; return {ok: false, code: 'NOT_FOUND', message: '接口不存在'};},
  }};
  const context = vm.createContext({browser, crypto: webcrypto, URL,
    Linkarium: {ORIGIN: 'https://link.eirawav.me', SITE_PATTERN: 'https://link.eirawav.me/*'}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dist/cloud.js'), 'utf8'), context);
  const response = await context.LocalCloud.request('/extension/submissions', {url: 'https://example.org/'});
  assert.equal(response.code, 'UPGRADE_REQUIRED');
  assert.match(response.message, /升级/);
  assert.equal(calls, 1);
});

test('validation shares the model transport mutex and rejects a duplicate validation', async () => {
  let release, entered, calls = 0;
  const started = new Promise(resolve => entered = resolve);
  const gate = new Promise(resolve => release = resolve);
  const env = environment({localAIConfig: config()}, async () => {calls++; entered(); if (calls === 1) await gate; return reply();});
  const running = env.context.LocalModel.complete(config(), [], {}, 120);
  await started;
  const validation = env.send({kind: 'local-ai-validate', config: config()});
  await tick();
  assert.equal(calls, 1, 'validation must wait for the ongoing classification transport');
  assert.equal((await env.send({kind: 'local-ai-validate', config: config()})).ok, false);
  release();
  await running;
  assert.equal((await validation).ok, true);
  assert.equal(calls, 2);
});

test('complete response storage failure pauses; explicit recovery uploads it without reinference', async () => {
  let failed = false, inference = 0, uploaded = 0, paused = false, followup = false;
  const claim = {protocol: 1, request_id: 'request', job_id: 'job', attempt_id: 'attempt', lease_token: 'lease',
    prompt_digest: 'digest', stage: 'classification', messages: [], schema: {}, timeout_seconds: 120};
  const values = {localAIConfig: {...config(), verified: true}};
  const env = environment(values, async () => {inference++; return reply('{"items":[]}');}, async (route, body) => {
    if (route.endsWith('/claim')) return {ok: true, data: !uploaded ? claim : followup && uploaded === 1 ? {...claim, request_id: 'next-request'} : null};
    if (route.endsWith('/pause')) {paused = body.error_code === 'LOCAL_STORAGE_UNAVAILABLE'; return {ok: true, data: {paused: true}};}
    if (route.endsWith('/result')) {uploaded++; return {ok: true, data: {auto_applied: false}};}
    throw new Error('unexpected route');
  }, update => {
    if (update.localAIWork?.response && !failed) {failed = true; return true;}
    return false;
  });
  for (let attempt = 0; attempt < 30 && !values.localAIPaused; attempt++) await tick();
  assert.equal(inference, 1);
  assert.equal(uploaded, 0);
  assert.equal(paused, true);
  assert.equal(values.localAIState.phase, 'storage_blocked');
  await env.send({kind: 'local-ai-resume'});
  assert.equal(inference, 1);
  assert.equal(uploaded, 1);
  assert.equal(values.localAIWork, undefined);
  followup = true;
  await env.context.LocalExecutor.wake();
  assert.equal(inference, 2, 'automatic wakes must continue after storage recovery');
  assert.equal(uploaded, 2);
});
