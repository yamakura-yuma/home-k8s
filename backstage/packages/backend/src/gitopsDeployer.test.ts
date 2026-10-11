import { ConfigReader } from '@backstage/config';
import { mockServices } from '@backstage/backend-test-utils';
import { createMockActionContext } from '@backstage/plugin-scaffolder-node-test-utils';
import {
  createGitopsDeployerActions,
  Deployment,
  GitopsDeployerClient,
  GitopsDeployerError,
  readSettings,
} from './gitopsDeployer';

const settings = {
  baseUrl: 'http://gateway/gitops-deployer/api/v1',
  tokenUrl: 'https://keycloak/realms/home-k8s/protocol/openid-connect/token',
  clientId: 'backstage-gitops-deployer',
  clientSecret: 'FAKE-client-value',
};

type Call = { url: string; init: RequestInit };

// Keycloak と bff の偽物。トークンの要求には expires_in 300 のトークンを、bff には routes の応答を順に返す
function fakeFetch(routes: Record<string, Array<{ status: number; body: unknown }>>) {
  const calls: Call[] = [];
  let tokens = 0;
  const impl = (async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    if (url === settings.tokenUrl) {
      tokens++;
      return new Response(JSON.stringify({ access_token: `tok${tokens}`, expires_in: 300 }), { status: 200 });
    }
    const key = `${init.method ?? 'GET'} ${url.replace(settings.baseUrl, '')}`;
    const queue = routes[key];
    if (!queue?.length) throw new Error(`想定外の呼び出し ${key}`);
    const r = queue.length > 1 ? queue.shift()! : queue[0];
    return new Response(JSON.stringify(r.body), { status: r.status });
  }) as unknown as typeof fetch;
  return { impl, calls, tokens: () => tokens };
}

const op = { workflowId: 'app-hello', runId: 'run1', app: 'hello', op: 'deploy', status: 'running', phase: 'opening-pr',
  links: { temporal: 'https://temporal/namespaces/gitops-deployer/workflows/app-hello/run1' } };
const running: Deployment = { name: 'hello', deployed: false, operation: { ...op, phase: 'waiting-for-argocd' } };
const done: Deployment = {
  name: 'hello', deployed: true,
  last: { op: 'deploy', result: 'succeeded', runId: 'run1' },
  operation: { ...op, status: 'completed', phase: undefined },
  links: { pr: 'https://github.com/yamakura-yuma/home-k8s/pull/9', argocd: 'https://argocd/applications/argocd/deploy-hello' },
};
const fast = { intervalMs: 1, timeoutMs: 60_000 };

describe('readSettings', () => {
  it('secret が無くても読める (アクションを使うときに失敗する)', () => {
    const s = readSettings(new ConfigReader({ gitopsDeployer: { ...settings, clientSecret: undefined, baseUrl: 'http://x/' } }));
    expect(s.baseUrl).toBe('http://x');
    expect(s.clientSecret).toBeUndefined();
  });
});

describe('GitopsDeployerClient', () => {
  it('client credentials のトークンを付け、期限の 1 分前まで使い回す', async () => {
    let now = 0;
    const f = fakeFetch({ 'GET /deployments/hello': [{ status: 200, body: done }] });
    const c = new GitopsDeployerClient(settings, f.impl, () => now);
    await c.get('hello');
    now = 239_000;
    await c.get('hello');
    expect(f.tokens()).toBe(1);
    now = 241_000;
    await c.get('hello');
    expect(f.tokens()).toBe(2);
    const form = new URLSearchParams(f.calls[0].init.body as string);
    expect(form.get('grant_type')).toBe('client_credentials');
    expect(form.get('client_id')).toBe('backstage-gitops-deployer');
    const bff = f.calls.filter(x => x.url !== settings.tokenUrl);
    expect((bff[0].init.headers as Record<string, string>).Authorization).toBe('Bearer tok1');
    expect((bff[2].init.headers as Record<string, string>).Authorization).toBe('Bearer tok2');
  });

  it('secret が無ければ bff を呼ばずに失敗する', async () => {
    const f = fakeFetch({});
    const c = new GitopsDeployerClient({ ...settings, clientSecret: undefined }, f.impl);
    await expect(c.get('hello')).rejects.toThrow('GITOPS_DEPLOYER_CLIENT_SECRET');
    expect(f.calls).toHaveLength(0);
  });

  it('problem+json の誤りは title・detail・欄ごとの誤りを文にする', async () => {
    const f = fakeFetch({ 'POST /deployments': [{ status: 422, body: {
      title: 'Unprocessable Content', detail: '入力が正しくない', errors: [{ field: 'image', detail: 'latest は不可' }] } }] });
    const c = new GitopsDeployerClient(settings, f.impl);
    const err = (await c.request('POST', '/deployments', { body: {} }).catch(e => e)) as GitopsDeployerError;
    expect(err).toBeInstanceOf(GitopsDeployerError);
    expect(err.status).toBe(422);
    expect(err.message).toContain('Unprocessable Content / 入力が正しくない / image: latest は不可');
  });

  it('一覧は全ページを読む', async () => {
    const f = fakeFetch({
      'GET /deployments?pageSize=100': [{ status: 200, body: { items: [{ name: 'a' }], nextPageToken: 'p2' } }],
      'GET /deployments?pageSize=100&pageToken=p2': [{ status: 200, body: { items: [{ name: 'b' }], nextPageToken: '' } }],
    });
    const c = new GitopsDeployerClient(settings, f.impl);
    const first = await c.list();
    const second = await c.list(first.nextPageToken);
    expect([...first.items, ...second.items].map(d => d.name)).toEqual(['a', 'b']);
  });
});

describe('gitops-deployer:deploy・delete', () => {
  it('deploy: フォームの値を POST し (Idempotency-Key はタスクの id)、Workflow が終わるまで待って結果とリンクを出す', async () => {
    const f = fakeFetch({
      'POST /deployments': [{ status: 202, body: op }],
      'GET /deployments/hello': [{ status: 200, body: running }, { status: 200, body: done }],
    });
    const [deploy] = createGitopsDeployerActions(new GitopsDeployerClient(settings, f.impl), fast);
    const ctx = createMockActionContext({
      input: { name: 'hello', image: 'docker.io/nginxinc/nginx-unprivileged:1.29-alpine', port: 8080, replicas: 1 },
      task: { id: 'task-1' },
    });
    await deploy.handler(ctx as any);
    const post = f.calls.find(x => x.init.method === 'POST' && x.url !== settings.tokenUrl)!;
    expect(JSON.parse(post.init.body as string)).toEqual({
      name: 'hello', image: 'docker.io/nginxinc/nginx-unprivileged:1.29-alpine', port: 8080, replicas: 1 });
    expect((post.init.headers as Record<string, string>)['Idempotency-Key']).toBe('backstage-task-1');
    expect(ctx.output).toHaveBeenCalledWith('result', 'succeeded');
    expect(ctx.output).toHaveBeenCalledWith('runId', 'run1');
    expect(ctx.output).toHaveBeenCalledWith('temporalUrl', op.links.temporal);
    expect(ctx.output).toHaveBeenCalledWith('prUrl', done.links!.pr);
    expect(ctx.output).toHaveBeenCalledWith('argocdUrl', done.links!.argocd);
  });

  it('delete: DELETE で依頼し、結果が failed ならタスクを失敗にする', async () => {
    const f = fakeFetch({
      'DELETE /deployments/hello': [{ status: 202, body: { ...op, op: 'delete' } }],
      'GET /deployments/hello': [{ status: 200, body: { ...done, last: { op: 'delete', result: 'failed', error: 'timeout', runId: 'run1' } } }],
    });
    const [, remove] = createGitopsDeployerActions(new GitopsDeployerClient(settings, f.impl), fast);
    const ctx = createMockActionContext({ input: { name: 'hello' }, task: { id: 'task-2' } });
    await expect(remove.handler(ctx as any)).rejects.toThrow('Workflow が失敗した: timeout');
    expect(ctx.output).toHaveBeenCalledWith('result', 'failed');
  });

  it('走っている間の依頼 (409) は bff の誤りのまま失敗する', async () => {
    const f = fakeFetch({ 'POST /deployments': [{ status: 409, body: { title: 'Conflict', detail: 'すでに走っている' } }] });
    const [deploy] = createGitopsDeployerActions(new GitopsDeployerClient(settings, f.impl), fast);
    const ctx = createMockActionContext({ input: { name: 'hello', image: 'docker.io/a/b:1' }, task: { id: 't' } });
    await expect(deploy.handler(ctx as any)).rejects.toThrow('409: Conflict / すでに走っている');
  });

  it('待ちきれなければ失敗する (Workflow は bff の側で続く)', async () => {
    const f = fakeFetch({
      'POST /deployments': [{ status: 202, body: op }],
      'GET /deployments/hello': [{ status: 200, body: running }],
    });
    const [deploy] = createGitopsDeployerActions(new GitopsDeployerClient(settings, f.impl), { intervalMs: 1, timeoutMs: 5 });
    const ctx = createMockActionContext({ input: { name: 'hello', image: 'docker.io/a/b:1' }, task: { id: 't' } });
    await expect(deploy.handler(ctx as any)).rejects.toThrow('Workflow が終わらない');
  });

  it('ロガーに段の移り変わりを出す', async () => {
    const logger = mockServices.logger.mock();
    const f = fakeFetch({
      'POST /deployments': [{ status: 202, body: op }],
      'GET /deployments/hello': [{ status: 200, body: running }, { status: 200, body: done }],
    });
    const [deploy] = createGitopsDeployerActions(new GitopsDeployerClient(settings, f.impl), fast);
    const ctx = createMockActionContext({ input: { name: 'hello', image: 'docker.io/a/b:1' }, task: { id: 't' }, logger });
    await deploy.handler(ctx as any);
    expect(logger.info).toHaveBeenCalledWith('段: waiting-for-argocd');
  });
});
