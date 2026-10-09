import { useCallback, useMemo } from 'react';
import useAsync from 'react-use/lib/useAsync';
import { ApiEntity, RELATION_PROVIDES_API } from '@backstage/catalog-model';
import {
  discoveryApiRef,
  identityApiRef,
  useApi,
} from '@backstage/core-plugin-api';
import {
  EmptyState,
  Progress,
  ResponseErrorPanel,
} from '@backstage/core-components';
import { useEntity, useRelatedEntities } from '@backstage/plugin-catalog-react';
import { OpenApiDefinitionWidget } from '@backstage/plugin-api-docs';
import { Environment, ENV_KEYS } from './annotations';
import { proxyServerUrl, withServer } from './openapi';

// Try it out の要求は、プロキシが GET だけ通す (app-config.yaml の allowedMethods)。他のメソッドの実行ボタンは出さない
const SUBMIT_METHODS = ['get'];

// Component の providesApis の API (openapi) の定義を、API エンティティへ移らずに Swagger UI で出す。
// Try it out の向き先は、選んだ環境のプロキシ (/api/proxy/sample-api-<環境>) に差し替える
export function SwaggerView({ env }: { env: Environment }) {
  const { entity } = useEntity();
  const discovery = useApi(discoveryApiRef);
  const { entities, loading, error } = useRelatedEntities(entity, {
    type: RELATION_PROVIDES_API,
    kind: 'API',
  });
  const proxyPath = env.values[ENV_KEYS.apiProxy];
  const { value: serverUrl, error: urlError } = useAsync(
    async () => proxyServerUrl(await discovery.getBaseUrl('proxy'), proxyPath),
    [discovery, proxyPath],
  );

  if (loading || (!serverUrl && !urlError)) return <Progress />;
  if (error ?? urlError)
    return <ResponseErrorPanel error={(error ?? urlError)!} />;
  const apis = (entities ?? []).filter(
    (api): api is ApiEntity => api.spec?.type === 'openapi',
  );
  if (apis.length === 0) {
    return (
      <EmptyState
        missing="content"
        title="OpenAPI の API がありません"
        description="Component の spec.providesApis に、spec.type が openapi の API エンティティを書く"
      />
    );
  }
  return (
    <>
      {apis.map(api => (
        <ApiSwagger
          key={`${api.metadata.namespace}/${api.metadata.name}`}
          api={api}
          env={env}
          serverUrl={serverUrl!}
        />
      ))}
    </>
  );
}

function ApiSwagger(props: {
  api: ApiEntity;
  env: Environment;
  serverUrl: string;
}) {
  const identity = useApi(identityApiRef);
  const { api, env, serverUrl } = props;
  const parsed = useMemo(() => {
    try {
      return {
        definition: withServer(api.spec.definition, {
          url: serverUrl,
          description: `${env.name} (Backstage のプロキシ経由)`,
        }),
      };
    } catch (e) {
      return { error: e as Error };
    }
  }, [api, env.name, serverUrl]);

  // swagger-ui の fetch は Backstage の fetchApi を通らないので、バックエンドの認証のトークンを自分で付ける。
  // 向き先 (プロキシ) への要求だけに付け、他の URL にはトークンを渡さない
  const requestInterceptor = useCallback(
    async (req: { url: string; headers: Record<string, string> }) => {
      if (req.url.startsWith(serverUrl)) {
        const { token } = await identity.getCredentials();
        if (token) req.headers.Authorization = `Bearer ${token}`;
      }
      return req;
    },
    [identity, serverUrl],
  );

  if (parsed.error) return <ResponseErrorPanel error={parsed.error} />;
  return (
    <OpenApiDefinitionWidget
      definition={parsed.definition}
      requestInterceptor={requestInterceptor}
      supportedSubmitMethods={SUBMIT_METHODS}
    />
  );
}
