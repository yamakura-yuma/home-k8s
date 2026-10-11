// gitops-deployer (services/gitops-deployer) を Backstage から使う。docs/cluster/gitops-deployer-backstage.md
//
// - Scaffolder のアクション gitops-deployer:deploy・gitops-deployer:delete。テンプレート (services/gitops-deployer/backstage) の
//   フォームの値で bff に依頼し、Workflow が終わるまで待って結果 (Temporal・PR・Argo CD のリンク) を出す
// - 一覧の中継 GET /api/gitops-deployer/deployments。カタログのページの gitops-deployer のタブが読む
//
// bff には Istio の Gateway → APISIX → bff の経路 (外の利用者と同じ入口) で届く。トークンは Keycloak の client
// backstage-gitops-deployer の client credentials で取り、期限の 1 分前まで使い回す。client の secret は
// `just up` が Secret backstage-gitops-deployer にし (just/gitops-deployer-secrets.sh)、chart が環境変数
// GITOPS_DEPLOYER_CLIENT_SECRET にする。無ければアクションと一覧は「資格情報が無い」で失敗する (Backstage は起動する)。
// トークンは Backstage のバックエンドの中だけで使い、ブラウザには渡さない。
import {
  coreServices,
  createBackendModule,
  createBackendPlugin,
  LoggerService,
} from '@backstage/backend-plugin-api';
import { Config } from '@backstage/config';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';
import { SpanStatusCode, trace } from '@opentelemetry/api';
import Router from 'express-promise-router';

// bff の Operation (services/gitops-deployer/internal/bff/openapi.yaml)
export type Operation = {
  workflowId: string;
  runId: string;
  app: string;
  op?: string;
  status: string;
  phase?: string;
  error?: string;
  links?: Record<string, string>;
};

// bff の Deployment。last は終わった Workflow の記録 (Dapr state の履歴)
export type Deployment = {
  name: string;
  deployed: boolean;
  current?: { image: string; port?: number; replicas?: number };
  last?: { op: string; result: string; error?: string; runId: string; requestedBy?: string; finishedAt?: string };
  operation?: Operation;
  links?: Record<string, string>;
};

export class GitopsDeployerError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

type Settings = {
  baseUrl: string;
  tokenUrl: string;
  clientId: string;
  clientSecret?: string;
};

export function readSettings(config: Config): Settings {
  return {
    baseUrl: config.getString('gitopsDeployer.baseUrl').replace(/\/+$/, ''),
    tokenUrl: config.getString('gitopsDeployer.tokenUrl'),
    clientId: config.getString('gitopsDeployer.clientId'),
    clientSecret: config.getOptionalString('gitopsDeployer.clientSecret'),
  };
}

// bff の呼び出し。トークンを付け、problem+json の誤りは detail を文にして投げる
export class GitopsDeployerClient {
  private token?: { value: string; expiresAt: number };

  constructor(
    private readonly settings: Settings,
    private readonly fetchImpl: typeof fetch = fetch,
    private readonly now: () => number = Date.now,
  ) {}

  private async accessToken(): Promise<string> {
    if (this.token && this.now() < this.token.expiresAt) {
      return this.token.value;
    }
    const { tokenUrl, clientId, clientSecret } = this.settings;
    if (!clientSecret) {
      throw new GitopsDeployerError(
        503,
        'gitopsDeployer.clientSecret (環境変数 GITOPS_DEPLOYER_CLIENT_SECRET) が無い。just up が Secret backstage-gitops-deployer を作る (docs/cluster/gitops-deployer-backstage.md)',
      );
    }
    const res = await this.fetchImpl(tokenUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'client_credentials',
        client_id: clientId,
        client_secret: clientSecret,
      }),
    });
    if (!res.ok) {
      throw new GitopsDeployerError(
        502,
        `Keycloak からトークンを取れない: ${res.status}`,
      );
    }
    const body = (await res.json()) as {
      access_token: string;
      expires_in: number;
    };
    this.token = {
      value: body.access_token,
      expiresAt: this.now() + (body.expires_in - 60) * 1000,
    };
    return body.access_token;
  }

  async request<T>(
    method: string,
    path: string,
    options: { body?: unknown; headers?: Record<string, string> } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${await this.accessToken()}`,
      ...options.headers,
    };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await this.fetchImpl(`${this.settings.baseUrl}${path}`, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const text = await res.text();
    if (!res.ok) {
      let detail = text;
      try {
        const p = JSON.parse(text);
        detail = [p.title, p.detail, ...(p.errors ?? []).map((e: { field: string; detail: string }) => `${e.field}: ${e.detail}`)]
          .filter(Boolean)
          .join(' / ');
      } catch {
        // problem+json でない (APISIX の 429 など) はそのまま
      }
      throw new GitopsDeployerError(res.status, `bff ${method} ${path} → ${res.status}: ${detail}`);
    }
    return JSON.parse(text) as T;
  }

  list(pageToken?: string) {
    const q = new URLSearchParams({ pageSize: '100' });
    if (pageToken) q.set('pageToken', pageToken);
    return this.request<{ items: Deployment[]; nextPageToken?: string }>(
      'GET',
      `/deployments?${q}`,
    );
  }

  get(name: string) {
    return this.request<Deployment>('GET', `/deployments/${encodeURIComponent(name)}`);
  }
}

const tracer = trace.getTracer('backstage-gitops-deployer');

type WaitOptions = { intervalMs: number; timeoutMs: number; signal?: AbortSignal };

// Workflow (runId) が終わるまで GET で待つ。段 (phase) が変わるたびに logger に出す。
// 終わった Workflow の結果は履歴 (last) にあり、runId が同じものだけを見る
export async function waitForWorkflow(
  client: GitopsDeployerClient,
  op: Operation,
  logger: LoggerService,
  { intervalMs, timeoutMs, signal }: WaitOptions,
): Promise<Deployment> {
  const deadline = Date.now() + timeoutMs;
  let lastPhase = op.phase;
  for (;;) {
    if (signal?.aborted) throw new Error('取り消された (Workflow は bff の側で続く)');
    const d = await client.get(op.app);
    const cur = d.operation;
    if (cur?.runId === op.runId && cur.status === 'running') {
      if (cur.phase && cur.phase !== lastPhase) {
        logger.info(`段: ${cur.phase}${cur.links?.pr ? ` (PR ${cur.links.pr})` : ''}`);
        lastPhase = cur.phase;
      }
    } else if (d.last?.runId === op.runId) {
      return d;
    } else if (cur?.runId === op.runId) {
      // 終わったが履歴にまだ無い (store-activity の前に止まった・補償など)
      throw new GitopsDeployerError(500, `Workflow が ${cur.status} で終わった${cur.error ? `: ${cur.error}` : ''}`);
    }
    if (Date.now() > deadline) {
      throw new GitopsDeployerError(504, `${timeoutMs / 60000} 分待っても Workflow が終わらない (${op.workflowId}/${op.runId})`);
    }
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

// Workflow の上限 (bff の WAIT_TIMEOUT 20 分) と補償の分を足した待ち時間
const WAIT: Omit<WaitOptions, 'signal'> = { intervalMs: 10_000, timeoutMs: 30 * 60_000 };

export function createGitopsDeployerActions(
  client: GitopsDeployerClient,
  wait: Omit<WaitOptions, 'signal'> = WAIT,
) {

  // 依頼して終わるまで待つ。Idempotency-Key は Scaffolder のタスクの id (同じタスクのやり直しで二重に依頼しない)。
  // span は bff の呼び出し (undici の計装、instrumentation.js) の親になり、traceparent で bff → … の trace に繋がる
  async function run(
    ctx: Parameters<Parameters<typeof createTemplateAction>[0]['handler']>[0],
    name: string,
    start: () => Promise<Operation>,
  ) {
    await tracer.startActiveSpan(`gitops-deployer ${ctx.templateInfo?.entityRef ?? ''}`.trim(), async span => {
      try {
        span.setAttributes({ 'gitops_deployer.app': name, 'backstage.task_id': ctx.task.id, 'backstage.user': ctx.user?.ref ?? '' });
        const op = await start();
        ctx.logger.info(`Workflow を始めた: ${op.workflowId}/${op.runId} (${op.op ?? ''})`);
        if (op.links?.temporal) ctx.logger.info(`Temporal: ${op.links.temporal}`);
        span.setAttribute('temporal.workflow_id', op.workflowId);
        const d = await waitForWorkflow(client, op, ctx.logger, { ...wait, signal: ctx.signal });
        const last = d.last!;
        ctx.output('workflowId', op.workflowId);
        ctx.output('runId', op.runId);
        ctx.output('result', last.result);
        const links = { ...op.links, ...d.links };
        if (links.temporal) ctx.output('temporalUrl', links.temporal);
        if (links.pr) ctx.output('prUrl', links.pr);
        if (links.argocd) ctx.output('argocdUrl', links.argocd);
        if (last.result === 'failed') {
          throw new GitopsDeployerError(500, `Workflow が失敗した: ${last.error ?? ''}`);
        }
        ctx.logger.info(`終わった: ${last.result}`);
      } catch (e) {
        span.recordException(e as Error);
        span.setStatus({ code: SpanStatusCode.ERROR, message: String(e) });
        throw e;
      } finally {
        span.end();
      }
    });
  }

  const deploy = createTemplateAction({
    id: 'gitops-deployer:deploy',
    description:
      'gitops-deployer の bff にデプロイを依頼し (deploys/<name>/ の PR → Argo CD)、Workflow が終わるまで待つ',
    schema: {
      input: {
        name: z => z.string().describe('アプリの名前 (Application deploy-<name>)'),
        image: z => z.string().describe('<レジストリ>/<リポジトリ>:<タグ>'),
        port: z => z.number().int().optional(),
        replicas: z => z.number().int().optional(),
      },
      output: {
        workflowId: z => z.string(),
        runId: z => z.string(),
        result: z => z.string(),
        temporalUrl: z => z.string().optional(),
        prUrl: z => z.string().optional(),
        argocdUrl: z => z.string().optional(),
      },
    },
    async handler(ctx) {
      const { name, image, port, replicas } = ctx.input;
      await run(ctx, name, () =>
        client.request<Operation>('POST', '/deployments', {
          body: { name, image, port, replicas },
          headers: { 'Idempotency-Key': `backstage-${ctx.task.id}` },
        }),
      );
    },
  });

  const remove = createTemplateAction({
    id: 'gitops-deployer:delete',
    description:
      'gitops-deployer の bff に削除を依頼し (deploys/<name>/ を消す PR → Application の消滅)、Workflow が終わるまで待つ',
    schema: {
      input: { name: z => z.string().describe('アプリの名前') },
      output: {
        workflowId: z => z.string(),
        runId: z => z.string(),
        result: z => z.string(),
        temporalUrl: z => z.string().optional(),
        prUrl: z => z.string().optional(),
        argocdUrl: z => z.string().optional(),
      },
    },
    async handler(ctx) {
      const { name } = ctx.input;
      await run(ctx, name, () =>
        client.request<Operation>('DELETE', `/deployments/${encodeURIComponent(name)}`, {
          headers: { 'Idempotency-Key': `backstage-${ctx.task.id}` },
        }),
      );
    },
  });

  return [deploy, remove];
}

export const scaffolderModuleGitopsDeployer = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'gitops-deployer',
  register(reg) {
    reg.registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint, config: coreServices.rootConfig },
      async init({ scaffolder, config }) {
        scaffolder.addActions(...createGitopsDeployerActions(new GitopsDeployerClient(readSettings(config))));
      },
    });
  },
});

// 一覧の中継。サインインした人 (ゲストを含む。読むだけ) に、bff の一覧 (全ページ) をそのまま返す
export const gitopsDeployerPlugin = createBackendPlugin({
  pluginId: 'gitops-deployer',
  register(env) {
    env.registerInit({
      deps: { httpRouter: coreServices.httpRouter, config: coreServices.rootConfig, logger: coreServices.logger },
      async init({ httpRouter, config, logger }) {
        const client = new GitopsDeployerClient(readSettings(config));
        const router = Router();
        router.get('/deployments', async (_req, res) => {
          try {
            const items: Deployment[] = [];
            let token: string | undefined;
            do {
              const page = await client.list(token);
              items.push(...page.items);
              token = page.nextPageToken || undefined;
            } while (token);
            res.json({ items });
          } catch (e) {
            const status = e instanceof GitopsDeployerError ? e.status : 500;
            logger.warn(`一覧を読めない: ${e}`);
            res.status(status).json({ error: { message: String((e as Error).message) } });
          }
        });
        httpRouter.use(router);
      },
    });
  },
});
