import useAsyncRetry from 'react-use/lib/useAsyncRetry';
import {
  discoveryApiRef,
  fetchApiRef,
  useApi,
} from '@backstage/core-plugin-api';
import {
  Link,
  ResponseErrorPanel,
  Table,
  TableColumn,
} from '@backstage/core-components';
import RefreshIcon from '@material-ui/icons/Refresh';

// gitops-deployer の注釈。値が true のエンティティ (services/gitops-deployer/catalog-info.yaml) にタブを出す
export const GITOPS_DEPLOYER_ANNOTATION = 'home-k8s/gitops-deployer';

// bff の Deployment (services/gitops-deployer/internal/bff/openapi.yaml) のうち、表に出すもの
export type Deployment = {
  name: string;
  deployed: boolean;
  current?: { image: string; port?: number; replicas?: number };
  last?: {
    op: string;
    result: string;
    requestedBy?: string;
    finishedAt?: string;
  };
  operation?: { op?: string; status: string; phase?: string };
  links?: Record<string, string>;
};

// 状態の 1 語。走っている Workflow があればその段、無ければ最後の Workflow の結果で決める
export function stateOf(d: Deployment): string {
  if (d.operation?.status === 'running') {
    return `${d.operation.op ?? '処理'} 中 (${d.operation.phase ?? '開始'})`;
  }
  return d.deployed ? 'デプロイ済み' : '削除済み';
}

const LINK_TITLES: Record<string, string> = {
  temporal: 'Temporal',
  pr: 'PR',
  argocd: 'Argo CD',
};

// リンクは http(s) だけを出す (bff の応答をそのまま href にしない)
export function safeLinks(links: Record<string, string> = {}) {
  return Object.entries(links).filter(([, url]) => /^https?:\/\//.test(url));
}

const columns: TableColumn<Deployment>[] = [
  { title: '名前', field: 'name', highlight: true },
  { title: '状態', render: d => stateOf(d) },
  { title: 'イメージ', render: d => d.current?.image ?? '' },
  {
    title: '最後の操作',
    render: d =>
      d.last
        ? `${d.last.op} ${d.last.result} (${d.last.requestedBy ?? ''} ${d.last.finishedAt ?? ''})`
        : '',
  },
  {
    title: 'リンク',
    render: d =>
      safeLinks(d.links).map(([key, url]) => (
        <span key={key} style={{ marginRight: 8 }}>
          <Link to={url}>{LINK_TITLES[key] ?? key}</Link>
        </span>
      )),
  },
];

// デプロイ済みの一覧と状態。Backstage のバックエンドの中継 (/api/gitops-deployer/deployments) が bff の一覧 API を
// Keycloak のトークン付きで読んで返す (packages/backend/src/gitopsDeployer.ts、docs/cluster/gitops-deployer-backstage.md)
export function GitopsDeployerView() {
  const discovery = useApi(discoveryApiRef);
  const { fetch } = useApi(fetchApiRef);
  const { value, loading, error, retry } = useAsyncRetry(async () => {
    const base = await discovery.getBaseUrl('gitops-deployer');
    const res = await fetch(`${base}/deployments`);
    if (!res.ok) {
      const body = await res.json().catch(() => undefined);
      throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
    }
    return ((await res.json()) as { items: Deployment[] }).items;
  }, [discovery, fetch]);

  if (error) return <ResponseErrorPanel error={error} />;
  return (
    <Table<Deployment>
      title="デプロイ済みの一覧 (gitops-deployer の bff)"
      subtitle="デプロイ・削除は Create の gitops-deployer のテンプレートから"
      isLoading={loading}
      columns={columns}
      data={value ?? []}
      options={{ paging: false, search: false }}
      actions={[
        {
          icon: () => <RefreshIcon />,
          tooltip: '読み直す',
          isFreeAction: true,
          onClick: () => retry(),
        },
      ]}
    />
  );
}
