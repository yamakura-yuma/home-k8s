import { compatWrapper } from '@backstage/core-compat-api';
import { EntityProvider, useEntity } from '@backstage/plugin-catalog-react';
import { EntityGrafanaDashboardsCard } from '@backstage-community/plugin-grafana';
import { Environment, ENV_KEYS, entityForEnvironment } from './annotations';

// Grafana プラグインの注釈 grafana/host-id と grafana/dashboard-selector は 1 つしか書けないので、選んだ環境の
// host の id とタグ (注釈 home-k8s/env.<環境>.grafana-host-id・grafana-dashboard-selector) を重ねたエンティティを渡し、
// プラグインのカードをそのまま出す。host ごとのプロキシ (app-config.yaml の grafana.hosts と proxy.endpoints./grafana-<環境>/api) が
// 環境ごとの Grafana に振り分け、資格情報はバックエンドのプロキシが持つ。iframe にしない。
// プラグインのカードは旧 API (createComponentExtension) なので compatWrapper で包む
export function GrafanaView({ env }: { env: Environment }) {
  const { entity } = useEntity();
  const scoped = entityForEnvironment(entity, env, {
    'grafana/host-id': ENV_KEYS.grafanaHostId,
    'grafana/dashboard-selector': ENV_KEYS.grafanaDashboardSelector,
  });
  return compatWrapper(
    <EntityProvider entity={scoped}>
      <EntityGrafanaDashboardsCard
        title={`${env.name} の Grafana のダッシュボード`}
      />
    </EntityProvider>,
  );
}
