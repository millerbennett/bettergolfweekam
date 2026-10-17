// This Worker only contacts GitHub. The paced scraper stays in Actions.
export const DAYTIME = '*/20 10-23 * * *';
export const OVERNIGHT = '25 8 * * *';
const WORKFLOW = 'https://api.github.com/repos/millerbennett/bettergolfweekam/actions/workflows/update.yml';
const MINUTE = 60_000;

export async function tick(event, env, fetcher = fetch, now = Date.now()) {
  if (env.ENABLED !== 'true') return 'disabled';
  if (![DAYTIME, OVERNIGHT].includes(event.cron)) throw new Error('Unexpected cron');
  // Never replay missed ticks or turn a delayed delivery into a catch-up burst.
  const age = now - event.scheduledTime;
  if (!Number.isFinite(age) || age < 0 || age > 5 * MINUTE) return 'stale tick';
  if (!env.GITHUB_TOKEN) throw new Error('Missing GitHub credential');
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'bettergolfweekam-scheduler',
    'Content-Type': 'application/json',
  };
  const request = (url, options = {}) => fetcher(url, {
    // Workers supports manual/follow, not Node's redirect: 'error'. A redirect
    // returns a non-OK response and fails closed without forwarding credentials.
    ...options, headers, signal: AbortSignal.timeout(10_000), redirect: 'manual',
  });
  const response = await request(`${WORKFLOW}/runs?branch=main&per_page=30`);
  if (!response.ok) throw new Error(`GitHub run check failed: ${response.status}`);
  const { workflow_runs: runs } = await response.json();
  if (!Array.isArray(runs)) throw new Error('Invalid GitHub run response');
  // Fail closed: a run that is queued, waiting, or active prevents another one.
  if (runs.some(run => run.status !== 'completed')) return 'already active';
  if (runs.some(run => now - Date.parse(run.created_at) < 18 * MINUTE)) return 'recent run';
  const result = await request(`${WORKFLOW}/dispatches`, {
    method: 'POST',
    body: JSON.stringify({
      ref: 'main',
      inputs: {
        mode: event.cron === OVERNIGHT ? 'daily' : 'auto',
        force: false,
        source: 'cloudflare',
        scheduled_at: new Date(event.scheduledTime).toISOString(),
      },
    }),
  });
  // No automatic retry, including ambiguous timeouts after GitHub accepted it.
  if (!result.ok) throw new Error(`GitHub dispatch failed: ${result.status}`);
  return 'dispatched';
}

export default {
  async scheduled(event, env) {
    const outcome = await tick(event, env);
    console.log(JSON.stringify({ scheduled_at: event.scheduledTime, cron: event.cron, outcome }));
  },
};
