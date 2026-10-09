import useAsync from 'react-use/lib/useAsync';
import { EntityProvider, useEntity } from '@backstage/plugin-catalog-react';
import {
  discoveryApiRef,
  fetchApiRef,
  useApi,
} from '@backstage/core-plugin-api';
import { EmptyState, Progress } from '@backstage/core-components';
import { compatWrapper } from '@backstage/core-compat-api';
import { EntityAzureSitesOverviewWidget } from '@backstage-community/plugin-azure-sites';
import {
  Environment,
  ENV_KEYS,
  entityForEnvironment,
} from '../environments/annotations';

// azure-sites プラグインが読む注釈。環境の値 (azure-web-sites) をこの注釈に重ねて渡す
export const AZURE_WEB_SITES_ANNOTATION = 'azure.com/microsoft-web-sites';

type Status = 'ready' | 'not-configured';

// バックエンドの /health で、資格情報があるかを見る。資格情報が無いと、バックエンドは代役のプラグイン
// (packages/backend/src/azureSites.ts) を載せ、/health が 503 を返す。公式のプラグインなら 200
export function useAzureStatus() {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  return useAsync(async (): Promise<Status> => {
    const base = await discovery.getBaseUrl('azure-sites');
    const res = await fetch(`${base}/health`);
    if (res.status === 503) return 'not-configured';
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return 'ready';
  }, [discovery, fetch]);
}

export function AzureSitesView({ env }: { env: Environment }) {
  const { entity } = useEntity();
  const { value, loading, error } = useAzureStatus();

  if (loading) return <Progress />;
  if (error) {
    return (
      <EmptyState
        missing="data"
        title="Azure のバックエンドに届きません"
        description={error.message}
      />
    );
  }
  if (value === 'not-configured') {
    return (
      <EmptyState
        missing="info"
        title="Azure の資格情報が無い"
        description={`~/.local/share/home-k8s/backstage/azure.env に読み取り用のサービスプリンシパルの資格情報を置いて just up を打つ (${env.name} の ${env.values[ENV_KEYS.azureWebSites]} は、資格情報ができたら出る。手順は docs/cluster/backstage-azure.md)`}
      />
    );
  }
  // 1 つのインスタンス前提のプラグインに、環境の値を注釈として渡す (docs/cluster/environments.md)
  const scoped = entityForEnvironment(entity, env, {
    [AZURE_WEB_SITES_ANNOTATION]: ENV_KEYS.azureWebSites,
  });
  return (
    <EntityProvider entity={scoped}>
      {compatWrapper(<EntityAzureSitesOverviewWidget />)}
    </EntityProvider>
  );
}
