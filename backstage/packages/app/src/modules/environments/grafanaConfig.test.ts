import fs from 'fs';
import path from 'path';
import { load, loadAll } from 'js-yaml';

// リポジトリの設定ファイルどうしの突き合わせ。Grafana の host の増やし方 (docs/cluster/backstage-grafana.md) が崩れると、
// 既存の home-k8s のカードが別の Grafana を向く・起動時にプラグインが落ちる・プロキシに資格情報が入らない、になる
const repo = path.resolve(__dirname, '../../../../../..');
const read = (file: string) => fs.readFileSync(path.join(repo, file), 'utf8');
const appConfig = load(read('backstage/app-config.yaml')) as any;
const hosts: any[] = appConfig.grafana.hosts;

describe('app-config.yaml の grafana', () => {
  it('host は default・dev・prod で、注釈の無いエンティティは default (いままでの Grafana) を向く', () => {
    expect(hosts.map(h => h.id)).toEqual(['default', 'dev', 'prod']);
    expect(appConfig.grafana.defaultHost).toBe('default');
    // grafana.hosts を書くと grafana.domain は無視される (プラグインの警告)。書き残さない
    expect(appConfig.grafana.domain).toBeUndefined();
    const byId = Object.fromEntries(hosts.map(h => [h.id, h]));
    expect(byId.default).toMatchObject({
      domain: 'http://localhost:3000',
      proxyPath: '/grafana/api',
    });
    expect(byId.dev.domain).toBe('http://localhost:3001');
    expect(byId.prod.domain).toBe('http://localhost:3002');
  });

  it('host ごとのプロキシの経路は別々で、どれも proxy.endpoints にある', () => {
    const paths = hosts.map(h => h.proxyPath);
    expect(new Set(paths).size).toBe(paths.length);
    for (const p of paths) expect(appConfig.proxy.endpoints).toHaveProperty([p]);
  });

  it('環境のプロキシは Secret の環境変数から Basic 認証を入れ、GET だけを通す', () => {
    for (const [env, name] of [
      ['dev', 'GRAFANA_DEV_BASIC_AUTH'],
      ['prod', 'GRAFANA_PROD_BASIC_AUTH'],
    ]) {
      const endpoint = appConfig.proxy.endpoints[`/grafana-${env}/api`];
      expect(endpoint.target).toBe(`http://grafana.${env}.svc.cluster.local`);
      expect(endpoint.headers.Authorization).toBe(`Basic \${${name}}`);
      expect(endpoint.allowedMethods).toEqual(['GET']);
      // 資格情報はファイルに書かない (環境変数の参照だけ)
      expect(JSON.stringify(endpoint)).not.toMatch(/Basic [A-Za-z0-9+/=]{8,}/);
      // just up が作る Secret (just/grafana-env-secrets.sh) のキーと同じ名前
      expect(read('just/grafana-env-secrets.sh')).toMatch(/GRAFANA_\$\(.*\)_BASIC_AUTH/);
    }
    const values = load(read('clusters/kind/backstage/values.yaml')) as any;
    expect(values.backstage.extraEnvVarsSecrets).toEqual(
      expect.arrayContaining(['backstage-grafana', 'backstage-grafana-env']),
    );
  });
});

describe('エンティティの注釈', () => {
  const entities = (file: string) => loadAll(read(file)) as any[];

  it('home-k8s は grafana/host-id を持たない (default の Grafana のカードがそのまま出る)', () => {
    const home = entities('catalog-info.yaml').find(
      e => e?.kind === 'Component' && e.metadata.name === 'home-k8s',
    );
    expect(home.metadata.annotations['grafana/dashboard-selector']).toBeTruthy();
    expect(home.metadata.annotations).not.toHaveProperty(['grafana/host-id']);
  });

  it('sample-api は環境ごとの注釈だけで、grafana/* を持たない (観測スタックの Grafana の既定のカードを出さない)', () => {
    const api = entities('services/sample-api/catalog-info.yaml').find(
      e => e?.kind === 'Component',
    );
    const annotations: Record<string, string> = api.metadata.annotations;
    expect(Object.keys(annotations).filter(k => k.startsWith('grafana/'))).toEqual([]);
    for (const env of ['dev', 'prod']) {
      // host の id は app-config.yaml の grafana.hosts にある
      expect(hosts.map(h => h.id)).toContain(
        annotations[`home-k8s/env.${env}.grafana-host-id`],
      );
      expect(annotations[`home-k8s/env.${env}.grafana-dashboard-selector`]).toBe('sample-api');
    }
  });

  it('環境の Grafana のダッシュボードは、選び方のタグ sample-api を持つ', () => {
    const dashboard = JSON.parse(
      read('clusters/kind/env-grafana/dashboards/sample-api.json'),
    );
    expect(dashboard.tags).toContain('sample-api');
    // ConfigMap にならない JSON は Grafana に入らない。kustomization.yaml の files に並べてある
    const kustomization = load(
      read('clusters/kind/env-grafana/dashboards/kustomization.yaml'),
    ) as any;
    expect(kustomization.configMapGenerator[0].files).toContain('sample-api.json');
  });
});
