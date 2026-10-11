import { renderInTestApp } from '@backstage/frontend-test-utils';
import { discoveryApiRef, fetchApiRef } from '@backstage/core-plugin-api';
import '@testing-library/jest-dom';
import { screen } from '@testing-library/react';
import {
  Deployment,
  GitopsDeployerView,
  safeLinks,
  stateOf,
} from './GitopsDeployerView';

// bff の一覧の形 (services/gitops-deployer/internal/bff/openapi.yaml の Deployment)
const items: Deployment[] = [
  {
    name: 'hello',
    deployed: true,
    current: { image: 'docker.io/nginxinc/nginx-unprivileged:1.29-alpine' },
    last: {
      op: 'deploy',
      result: 'succeeded',
      requestedBy: 'service-account-backstage-gitops-deployer',
      finishedAt: '2026-10-12T00:00:00Z',
    },
    links: {
      argocd: 'https://argocd.taild2b611.ts.net/applications/argocd/deploy-hello',
      pr: 'javascript:alert(1)',
    },
  },
  {
    name: 'busy',
    deployed: false,
    operation: { op: 'deploy', status: 'running', phase: 'waiting-for-argocd' },
  },
];

function apis(response: Response) {
  const fetch = jest.fn(async () => response);
  return {
    fetch,
    apis: [
      [discoveryApiRef, { getBaseUrl: async (id: string) => `http://backstage/api/${id}` }],
      [fetchApiRef, { fetch }],
    ] as const,
  };
}

describe('stateOf', () => {
  it('走っている Workflow があればその段、無ければデプロイ済みか削除済み', () => {
    expect(stateOf(items[0])).toBe('デプロイ済み');
    expect(stateOf(items[1])).toBe('deploy 中 (waiting-for-argocd)');
    expect(stateOf({ name: 'gone', deployed: false })).toBe('削除済み');
  });
});

describe('safeLinks', () => {
  it('http(s) のリンクだけを残す', () => {
    expect(safeLinks(items[0].links)).toEqual([
      ['argocd', 'https://argocd.taild2b611.ts.net/applications/argocd/deploy-hello'],
    ]);
  });
});

describe('GitopsDeployerView', () => {
  it('バックエンドの中継 (/api/gitops-deployer/deployments) の一覧を表にする', async () => {
    const { fetch, apis: a } = apis(
      new Response(JSON.stringify({ items }), { status: 200 }),
    );
    await renderInTestApp(<GitopsDeployerView />, { apis: a as any });
    expect(await screen.findByText('hello')).toBeInTheDocument();
    expect(screen.getByText('デプロイ済み')).toBeInTheDocument();
    expect(screen.getByText('deploy 中 (waiting-for-argocd)')).toBeInTheDocument();
    expect(screen.getByText('Argo CD')).toBeInTheDocument();
    expect(screen.queryByText('PR')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith('http://backstage/api/gitops-deployer/deployments');
  });

  it('中継の誤り (資格情報が無いなど) はそのまま出す', async () => {
    const { apis: a } = apis(
      new Response(JSON.stringify({ error: { message: '資格情報が無い' } }), { status: 503 }),
    );
    await renderInTestApp(<GitopsDeployerView />, { apis: a as any });
    expect(await screen.findByText('Error: 資格情報が無い')).toBeInTheDocument();
  });
});
