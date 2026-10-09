import { Entity } from '@backstage/catalog-model';
import { renderInTestApp } from '@backstage/frontend-test-utils';
import { EntityProvider } from '@backstage/plugin-catalog-react';
import { grafanaApiRef } from '@backstage-community/plugin-grafana';
import '@testing-library/jest-dom';
import { fireEvent, screen } from '@testing-library/react';
import { ENV_KEYS } from './annotations';
import { EnvironmentSwitcher } from './EnvironmentSwitcher';
import { GrafanaView } from './GrafanaView';

// エンティティ自身には grafana/* を書かない。環境ごとの注釈だけ
const entity: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Component',
  metadata: {
    name: 'sample-api',
    annotations: {
      'home-k8s/environments': 'dev,prod',
      'home-k8s/env.dev.grafana-host-id': 'dev',
      'home-k8s/env.dev.grafana-dashboard-selector': 'sample-api',
      'home-k8s/env.prod.grafana-host-id': 'prod',
      'home-k8s/env.prod.grafana-dashboard-selector': 'sample-api',
    },
  },
};

// Grafana の API の代わり。host の id ごとのダッシュボード (本物は /grafana-<環境>/api/search をプロキシ経由で読む)
const dashboards: Record<string, string> = {
  dev: 'dev のダッシュボード',
  prod: 'prod のダッシュボード',
};
const grafanaApi = {
  listDashboards: jest.fn(async (_query: string, hostId?: string) => [
    {
      title: dashboards[hostId ?? 'default'],
      url: `http://grafana-${hostId}/d/x`,
      folderTitle: '',
      folderUrl: '',
      tags: ['sample-api'],
    },
  ]),
  isUnifiedAlerting: jest.fn(() => false),
};

function renderTab() {
  return renderInTestApp(
    <EntityProvider entity={entity}>
      <EnvironmentSwitcher
        requires={[ENV_KEYS.grafanaHostId, ENV_KEYS.grafanaDashboardSelector]}
      >
        {env => <GrafanaView env={env} />}
      </EnvironmentSwitcher>
    </EntityProvider>,
    { apis: [[grafanaApiRef, grafanaApi]] },
  );
}

describe('GrafanaView', () => {
  beforeEach(() => grafanaApi.listDashboards.mockClear());

  it('dev の host のダッシュボードを出し、prod に切り替えると prod の host のものを出す', async () => {
    await renderTab();
    expect(await screen.findByText('dev のダッシュボード')).toBeInTheDocument();
    // 選び方のタグと host の id が、環境の注釈から重なる
    expect(grafanaApi.listDashboards).toHaveBeenCalledWith('sample-api', 'dev');

    fireEvent.click(screen.getByRole('tab', { name: 'prod' }));
    expect(await screen.findByText('prod のダッシュボード')).toBeInTheDocument();
    expect(screen.queryByText('dev のダッシュボード')).not.toBeInTheDocument();
    expect(grafanaApi.listDashboards).toHaveBeenLastCalledWith('sample-api', 'prod');
  });
});
