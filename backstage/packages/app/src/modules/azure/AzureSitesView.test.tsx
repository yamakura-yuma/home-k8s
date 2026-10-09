import { Entity } from '@backstage/catalog-model';
import {
  DiscoveryApi,
  discoveryApiRef,
  FetchApi,
  fetchApiRef,
} from '@backstage/core-plugin-api';
import { catalogApiRef, useEntity } from '@backstage/plugin-catalog-react';
import {
  renderInTestApp,
  TestApiProvider,
} from '@backstage/frontend-test-utils';
import { AzureSitesView, AZURE_WEB_SITES_ANNOTATION } from './AzureSitesView';

// 部品は azure-sites 本体ではなく、渡された注釈を出すだけのものにする (本体は Azure のバックエンドを呼ぶ)
jest.mock('@backstage-community/plugin-azure-sites', () => {
  const { useEntity: useScopedEntity } = jest.requireActual(
    '@backstage/plugin-catalog-react',
  );
  return {
    EntityAzureSitesOverviewWidget: () => {
      const { entity } = useScopedEntity();
      return (
        <div>
          azure-sites の注釈:{' '}
          {entity.metadata.annotations?.['azure.com/microsoft-web-sites']}
        </div>
      );
    },
  };
});

const entity: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Component',
  metadata: { name: 'sample-api' },
};
const dev = {
  name: 'dev',
  values: { 'azure-web-sites': 'sample-api-dev' },
};

function Probe() {
  const { entity: e } = useEntity();
  return <AzureSitesView env={dev} key={e.metadata.name} />;
}

async function renderWith(health: { status: number }) {
  const fetch = jest.fn(async () => ({
    ...health,
    ok: health.status < 400,
    statusText: '',
  }));
  const discovery: Partial<DiscoveryApi> = {
    getBaseUrl: async (id: string) => `http://backend/api/${id}`,
  };
  const { EntityProvider } = await import('@backstage/plugin-catalog-react');
  const result = await renderInTestApp(
    <TestApiProvider
      apis={[
        [discoveryApiRef, discovery as DiscoveryApi],
        [fetchApiRef, { fetch } as unknown as FetchApi],
        [catalogApiRef, { getEntities: async () => ({ items: [] }) } as any],
      ]}
    >
      <EntityProvider entity={entity}>
        <Probe />
      </EntityProvider>
    </TestApiProvider>,
  );
  return { ...result, fetch };
}

describe('AzureSitesView', () => {
  it('バックエンドの /health が 503 なら、資格情報が無い旨を出し、プラグインの部品は出さない', async () => {
    const { findByText, queryByText, fetch } = await renderWith({
      status: 503,
    });
    expect(await findByText('Azure の資格情報が無い')).not.toBeNull();
    expect(fetch).toHaveBeenCalledWith('http://backend/api/azure-sites/health');
    expect(queryByText(/azure-sites の注釈/)).toBeNull();
  });

  it('/health が 200 なら、環境の azure-web-sites をプラグインの注釈に重ねて部品を出す', async () => {
    const { findByText, queryByText } = await renderWith({ status: 200 });
    expect(
      await findByText(`azure-sites の注釈: sample-api-dev`, { exact: false }),
    ).not.toBeNull();
    expect(queryByText('Azure の資格情報が無い')).toBeNull();
    expect(AZURE_WEB_SITES_ANNOTATION).toBe('azure.com/microsoft-web-sites');
  });

  it('/health が 500 なら、資格情報が無いとは言わずバックエンドの失敗として出す', async () => {
    const { findByText, queryByText } = await renderWith({ status: 500 });
    expect(await findByText('Azure のバックエンドに届きません')).not.toBeNull();
    expect(queryByText('Azure の資格情報が無い')).toBeNull();
  });
});
