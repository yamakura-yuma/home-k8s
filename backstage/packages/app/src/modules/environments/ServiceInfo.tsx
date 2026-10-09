import useAsync from 'react-use/lib/useAsync';
import {
  discoveryApiRef,
  fetchApiRef,
  useApi,
} from '@backstage/core-plugin-api';
import {
  InfoCard,
  Progress,
  ResponseErrorPanel,
  StructuredMetadataTable,
} from '@backstage/core-components';
import { Environment, ENV_KEYS } from './annotations';

// 環境のサービスの /info を、バックエンドのプロキシ (注釈 home-k8s/env.<環境>.api-proxy) を通して読んで出す
export function ServiceInfo({ env }: { env: Environment }) {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  const proxyPath = env.values[ENV_KEYS.apiProxy];
  const { value, loading, error } = useAsync(async () => {
    const base = await discovery.getBaseUrl('proxy');
    const res = await fetch(`${base}${proxyPath}/info`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return (await res.json()) as Record<string, string>;
  }, [discovery, fetch, proxyPath]);

  if (loading) return <Progress />;
  if (error) return <ResponseErrorPanel error={error} />;
  return (
    <InfoCard title={`${env.name} の /info`} subheader={`/api/proxy${proxyPath}`}>
      <StructuredMetadataTable metadata={value ?? {}} />
    </InfoCard>
  );
}
