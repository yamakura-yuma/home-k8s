import { ReactNode } from 'react';
import { screen, waitFor } from '@testing-library/react';
import { Entity } from '@backstage/catalog-model';
import { discoveryApiRef, identityApiRef } from '@backstage/core-plugin-api';
import { catalogApiRef, EntityProvider } from '@backstage/plugin-catalog-react';
import {
  mockApis,
  renderInTestApp,
  TestApiProvider,
} from '@backstage/frontend-test-utils';
import { SwaggerView } from './SwaggerView';

// swagger-ui は jsdom では重いので、widget に渡る値 (定義・インターセプタ・実行できるメソッド) だけを確かめる
const widgetProps: any[] = [];
jest.mock('@backstage/plugin-api-docs', () => ({
  OpenApiDefinitionWidget: (props: any) => {
    widgetProps.push(props);
    return <div data-testid="swagger">{props.definition}</div>;
  },
}));

const component: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Component',
  metadata: { name: 'sample-api' },
  spec: { providesApis: ['sample-api'] },
  relations: [{ type: 'providesApi', targetRef: 'api:default/sample-api' }],
};
const api: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'API',
  metadata: { name: 'sample-api', namespace: 'default' },
  spec: {
    type: 'openapi',
    definition:
      'openapi: 3.0.3\ninfo: {title: sample-api, version: 0.1.0}\nservers:\n  - url: /api/proxy/sample-api-dev\npaths: {}\n',
  },
};

function render(env: string, apis: Entity[] = [api], token = 'tok') {
  const catalog = {
    getEntitiesByRefs: jest.fn().mockResolvedValue({ items: apis }),
  };
  const wrap = (children: ReactNode) => (
    <TestApiProvider
      apis={[
        [catalogApiRef, catalog],
        [
          discoveryApiRef,
          { getBaseUrl: async () => 'http://localhost:7007/api/proxy' },
        ],
        [identityApiRef, mockApis.identity({ token })],
      ]}
    >
      <EntityProvider entity={component}>{children}</EntityProvider>
    </TestApiProvider>
  );
  return renderInTestApp(
    wrap(
      <SwaggerView
        env={{ name: env, values: { 'api-proxy': `/sample-api-${env}` } }}
      />,
    ),
  );
}

beforeEach(() => {
  widgetProps.length = 0;
});

describe('SwaggerView', () => {
  it.each(['dev', 'prod'])(
    '%s: providesApis の API の定義を、その環境のプロキシだけを servers にして出す',
    async env => {
      await render(env);
      await waitFor(() => expect(screen.getByTestId('swagger')).toBeTruthy());
      const spec = JSON.parse(widgetProps[widgetProps.length - 1].definition);
      expect(spec.servers).toEqual([
        {
          url: `http://localhost:7007/api/proxy/sample-api-${env}`,
          description: `${env} (Backstage のプロキシ経由)`,
        },
      ]);
      // プロキシは GET だけ通すので、実行できるのも GET だけ
      expect(
        widgetProps[widgetProps.length - 1].supportedSubmitMethods,
      ).toEqual(['get']);
    },
  );

  it('向き先の要求にだけ、バックエンドの認証のトークンを付ける', async () => {
    await render('dev');
    await waitFor(() => expect(widgetProps.length).toBeGreaterThan(0));
    const { requestInterceptor } = widgetProps[widgetProps.length - 1];
    const own = await requestInterceptor({
      url: 'http://localhost:7007/api/proxy/sample-api-dev/info',
      headers: {},
    });
    expect(own.headers.Authorization).toBe('Bearer tok');
    const other = await requestInterceptor({
      url: 'https://example.com/x',
      headers: {},
    });
    expect(other.headers.Authorization).toBeUndefined();
  });

  it('openapi の API が無ければ、案内を出す', async () => {
    await render('dev', [
      { ...api, spec: { type: 'graphql', definition: '' } },
    ]);
    await waitFor(() =>
      expect(screen.getByText('OpenAPI の API がありません')).toBeTruthy(),
    );
    expect(widgetProps).toHaveLength(0);
  });
});
