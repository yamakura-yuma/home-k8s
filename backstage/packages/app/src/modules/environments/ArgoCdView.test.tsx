import { Entity } from '@backstage/catalog-model';
import { renderInTestApp } from '@backstage/frontend-test-utils';
import { EntityProvider } from '@backstage/plugin-catalog-react';
import { argoCDApiRef } from '@roadiehq/backstage-plugin-argo-cd';
import '@testing-library/jest-dom';
import { fireEvent, screen } from '@testing-library/react';
import { ENV_KEYS } from './annotations';
import { ArgoCdView } from './ArgoCdView';
import { EnvironmentSwitcher } from './EnvironmentSwitcher';

const entity: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Component',
  metadata: {
    name: 'sample-api',
    annotations: {
      'home-k8s/environments': 'dev,prod',
      'home-k8s/env.dev.argocd-app-name': 'sample-api-dev',
      'home-k8s/env.prod.argocd-app-name': 'sample-api-prod',
    },
  },
};

// ArgoCD の API の代わり。Application の名前ごとの応答 (本物は /api/v1/applications/<名前> をプロキシ経由で読む)
const states: Record<string, { sync: string; health: string }> = {
  'sample-api-dev': { sync: 'Synced', health: 'Healthy' },
  'sample-api-prod': { sync: 'OutOfSync', health: 'Degraded' },
};
const argoCdApi = {
  getAppDetails: jest.fn(async ({ appName }: { appName: string }) => ({
    metadata: { name: appName, namespace: 'argocd', instance: undefined },
    status: {
      sync: { status: states[appName].sync },
      health: { status: states[appName].health },
      operationState: undefined,
      history: [],
    },
  })),
};

function renderTab() {
  return renderInTestApp(
    <EntityProvider entity={entity}>
      <EnvironmentSwitcher requires={[ENV_KEYS.argocdAppName]}>
        {env => <ArgoCdView env={env} />}
      </EnvironmentSwitcher>
    </EntityProvider>,
    { apis: [[argoCDApiRef, argoCdApi]] },
  );
}

describe('ArgoCdView', () => {
  beforeEach(() => argoCdApi.getAppDetails.mockClear());

  it('dev の Application の同期状態と健全性を出し、prod に切り替えると prod のものを出す', async () => {
    await renderTab();
    expect(await screen.findByText('Synced')).toBeInTheDocument();
    expect(screen.getByText('Healthy')).toBeInTheDocument();
    expect(argoCdApi.getAppDetails).toHaveBeenCalledWith(
      expect.objectContaining({ appName: 'sample-api-dev' }),
    );
    // 既定のプロキシの経路 (app-config.yaml の proxy.endpoints./argocd/api)
    expect(argoCdApi.getAppDetails).toHaveBeenCalledWith(
      expect.objectContaining({ url: '/argocd/api' }),
    );

    fireEvent.click(screen.getByRole('tab', { name: 'prod' }));
    expect(await screen.findByText('OutOfSync')).toBeInTheDocument();
    expect(screen.getByText('Degraded')).toBeInTheDocument();
    expect(screen.queryByText('Healthy')).not.toBeInTheDocument();
    expect(argoCdApi.getAppDetails).toHaveBeenCalledWith(
      expect.objectContaining({ appName: 'sample-api-prod' }),
    );
  });
});
