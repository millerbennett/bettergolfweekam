import { test } from 'node:test';
import assert from 'node:assert/strict';
import { tick, DAYTIME, OVERNIGHT } from './worker.mjs';

const now = Date.parse('2026-10-17T17:00:02Z');
const event = { cron: DAYTIME, scheduledTime: now - 2_000 };
const env = { ENABLED: 'true', GITHUB_TOKEN: 'test-only' };
function github(runs = [], dispatchStatus = 204) {
  const calls = [];
  return { calls, fetcher: async (url, options) => {
    calls.push({ url, ...options });
    if (options.method === 'POST') return new Response(null, { status: dispatchStatus });
    return Response.json({ workflow_runs: runs });
  } };
}

test('one tick dispatches exactly once, without contacting the golf site', async () => {
  const api = github();
  assert.equal(await tick(event, env, api.fetcher, now), 'dispatched');
  assert.equal(api.calls.length, 2);
  assert.ok(api.calls.every(c => new URL(c.url).hostname === 'api.github.com'));
  assert.ok(api.calls.every(c => c.redirect === 'manual'));
  assert.deepEqual(JSON.parse(api.calls[1].body), { ref: 'main', inputs: {
    mode: 'auto', force: false, source: 'cloudflare', scheduled_at: '2026-10-17T17:00:00.000Z',
  } });
});
test('overnight uses daily, never full or force', async () => {
  const api = github();
  await tick({ ...event, cron: OVERNIGHT }, env, api.fetcher, now);
  assert.equal(JSON.parse(api.calls[1].body).inputs.mode, 'daily');
  assert.equal(JSON.parse(api.calls[1].body).inputs.force, false);
});
test('disabled or stale timers make no requests', async () => {
  const api = github();
  assert.equal(await tick(event, { ...env, ENABLED: 'false' }, api.fetcher, now), 'disabled');
  assert.equal(await tick(event, env, api.fetcher, now + 6 * 60_000), 'stale tick');
  assert.equal(await tick({ ...event, scheduledTime: now + 1 }, env, api.fetcher, now), 'stale tick');
  assert.equal(api.calls.length, 0);
});
for (const status of ['queued', 'in_progress', 'waiting', 'pending']) {
  test(`${status} run prevents another dispatch`, async () => {
    const api = github([{ status, created_at: new Date(now - 30 * 60_000).toISOString() }]);
    assert.equal(await tick(event, env, api.fetcher, now), 'already active');
    assert.equal(api.calls.length, 1);
  });
}
test('recent successful or failed job suppresses duplicates', async () => {
  const api = github([{ status: 'completed', created_at: new Date(now - 60_000).toISOString() }]);
  assert.equal(await tick(event, env, api.fetcher, now), 'recent run');
  assert.equal(api.calls.length, 1);
});
test('check errors fail closed', async () => {
  let calls = 0;
  await assert.rejects(tick(event, env, async () => { calls++; return new Response(null, { status: 503 }); }, now));
  assert.equal(calls, 1);
});
test('failed dispatch is never retried', async () => {
  const api = github([], 500);
  await assert.rejects(tick(event, env, api.fetcher, now));
  assert.equal(api.calls.length, 2);
});
test('GitHub redirects are rejected without forwarding the credential', async () => {
  let calls = 0;
  await assert.rejects(tick(event, env, async () => {
    calls++;
    return new Response(null, { status: 302, headers: { Location: 'https://example.invalid' } });
  }, now));
  assert.equal(calls, 1);
});
test('malformed status response and missing credentials fail closed', async () => {
  await assert.rejects(tick(event, env, async () => Response.json({}), now));
  await assert.rejects(tick(event, { ENABLED: 'true' }, github().fetcher, now));
});
