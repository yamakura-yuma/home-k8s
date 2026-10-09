import { EmptyState } from '@backstage/core-components';
import { Environment, ENV_KEYS } from './annotations';

// 注釈の値が http(s) の URL ならそれを返す。それ以外 (javascript: など) は iframe に渡さない
export function temporalEmbedUrl(raw: string | undefined): string | undefined {
  try {
    const url = new URL(raw ?? '');
    return url.protocol === 'http:' || url.protocol === 'https:'
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

// 環境の Temporal Web UI (注釈 home-k8s/env.<環境>.temporal-url) を iframe で出す。
// Temporal 用のプラグインが npm に無いので iframe にする。Web UI は X-Frame-Options: SAMEORIGIN を返すので、
// 環境の proxy (clusters/kind/temporal/base/Caddyfile) がそれを外して frame-ancestors で Backstage だけに許し、
// Backstage の CSP の frame-src (app-config.yaml) もその URL を許す (docs/cluster/temporal.md)
export function TemporalView({ env }: { env: Environment }) {
  const url = temporalEmbedUrl(env.values[ENV_KEYS.temporalUrl]);
  if (!url) {
    return (
      <EmptyState
        missing="info"
        title="Temporal UI の URL がありません"
        description={`home-k8s/env.${env.name}.${ENV_KEYS.temporalUrl} に http(s) の URL を書く`}
      />
    );
  }
  return (
    <>
      <iframe
        title={`Temporal UI (${env.name})`}
        src={url}
        style={{
          width: '100%',
          height: 'calc(100vh - 280px)',
          minHeight: 480,
          border: 0,
        }}
      />
      <a href={url} target="_blank" rel="noreferrer">
        {env.name} の Temporal UI を別のタブで開く
      </a>
    </>
  );
}
