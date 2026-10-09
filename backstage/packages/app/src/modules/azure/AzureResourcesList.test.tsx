import { Entity } from '@backstage/catalog-model';
import { catalogApiRef } from '@backstage/plugin-catalog-react';
import {
  renderInTestApp,
  TestApiProvider,
} from '@backstage/frontend-test-utils';
import { entityRouteRef } from '@backstage/plugin-catalog-react';
import { AzureResourcesList } from './AzureResourcesList';

const sampleApi: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Component',
  metadata: { name: 'sample-api' },
};

const storage: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Resource',
  metadata: {
    name: 'sampleapidevstore',
    namespace: 'dev',
    annotations: {
      'management.azure.com/location': 'japaneast',
      'backstage.io/view-url': 'https://portal.azure.com/#@t/resource/x',
    },
  },
  spec: { type: 'microsoft.storage/storageaccounts', owner: 'user:default/a' },
};

async function renderList(env: string, items: Entity[]) {
  const getEntities = jest.fn(async () => ({ items }));
  const result = await renderInTestApp(
    <TestApiProvider apis={[[catalogApiRef, { getEntities } as any]]}>
      <AzureResourcesList entity={sampleApi} env={{ name: env, values: {} }} />
    </TestApiProvider>,
    { mountedRoutes: { '/catalog/:namespace/:kind/:name': entityRouteRef } },
  );
  return { ...result, getEntities };
}

describe('AzureResourcesList', () => {
  it('選んだ環境の namespace で、sample-api に dependencyOf で結ばれた Resource をカタログに問い合わせる', async () => {
    const { getEntities, findByText } = await renderList('dev', [storage]);
    expect(await findByText('sampleapidevstore')).not.toBeNull();
    expect(getEntities).toHaveBeenCalledWith({
      filter: {
        kind: 'Resource',
        'metadata.namespace': 'dev',
        'relations.dependencyOf': 'component:default/sample-api',
      },
    });
  });

  it('種類・場所・ポータルへのリンクを出す', async () => {
    const { findByText, findByRole } = await renderList('dev', [storage]);
    expect(
      await findByText('microsoft.storage/storageaccounts'),
    ).not.toBeNull();
    expect(await findByText('japaneast')).not.toBeNull();
    expect(
      (await findByRole('link', { name: /ポータル/ })).getAttribute('href'),
    ).toBe('https://portal.azure.com/#@t/resource/x');
  });

  it('Resource が無ければ、付けるタグを出す', async () => {
    const { findByText } = await renderList('prod', []);
    expect(
      await findByText(/タグ environment=prod・service=sample-api/),
    ).not.toBeNull();
  });
});
