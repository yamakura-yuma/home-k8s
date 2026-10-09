import { ReactNode } from 'react';
import { createFrontendPlugin } from '@backstage/frontend-plugin-api';
import { EntityContentBlueprint } from '@backstage/plugin-catalog-react/alpha';
import { Environment, ENV_KEYS, environmentsWith } from './annotations';
import { EnvironmentSwitcher } from './EnvironmentSwitcher';
import { ArgoCdView } from './ArgoCdView';
import { ServiceInfo } from './ServiceInfo';
import { SwaggerView } from './SwaggerView';
import { TemporalView } from './TemporalView';

// 環境 (dev・prod) を切り替えて中身を出すエンティティのタブを作る。
// requires は、そのタブが使う環境のキー (annotations.ts の ENV_KEYS)。どの環境にも揃っていないエンティティにはタブを出さない。
// Temporal・Grafana・ArgoCD・Azure のタブも、これで作って下の plugin の extensions に足す (docs/cluster/environments.md)
export function createEnvironmentContent(options: {
  name: string;
  path: string;
  title: string;
  requires: readonly string[];
  render: (env: Environment) => ReactNode;
}) {
  return EntityContentBlueprint.make({
    name: options.name,
    params: {
      path: options.path,
      title: options.title,
      filter: entity => environmentsWith(entity, options.requires).length > 0,
      loader: async () => (
        <EnvironmentSwitcher requires={options.requires}>
          {options.render}
        </EnvironmentSwitcher>
      ),
    },
  });
}

// サンプルの API の /info を環境ごとに出すタブ (拡張 entity-content:environments/service)
const serviceContent = createEnvironmentContent({
  name: 'service',
  path: '/environments',
  title: 'Environments',
  requires: [ENV_KEYS.apiProxy],
  render: env => <ServiceInfo env={env} />,
});

// サンプルの API の OpenAPI を、環境のプロキシを向き先にして Swagger UI で出すタブ (拡張 entity-content:environments/swagger)
const swaggerContent = createEnvironmentContent({
  name: 'swagger',
  path: '/swagger',
  title: 'Swagger',
  requires: [ENV_KEYS.apiProxy],
  render: env => <SwaggerView env={env} />,
});

// 環境ごとの Application (sample-api-dev・sample-api-prod) の同期状態と健全性 (拡張 entity-content:environments/argocd)
const argocdContent = createEnvironmentContent({
  name: 'argocd',
  path: '/argocd',
  title: 'ArgoCD',
  requires: [ENV_KEYS.argocdAppName],
  render: env => <ArgoCdView env={env} />,
});

// 環境の Temporal Web UI を iframe で出すタブ (拡張 entity-content:environments/temporal)
const temporalContent = createEnvironmentContent({
  name: 'temporal',
  path: '/temporal',
  title: 'Temporal',
  requires: [ENV_KEYS.temporalUrl],
  render: env => <TemporalView env={env} />,
});

export const environmentsPlugin = createFrontendPlugin({
  pluginId: 'environments',
  extensions: [serviceContent, swaggerContent, argocdContent, temporalContent],
});
