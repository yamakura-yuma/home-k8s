import { load } from 'js-yaml';

// OpenAPI の servers を、Try it out の向き先 1 つ (環境のプロキシ) に差し替える。
// 定義は API エンティティの spec.definition (サービスの openapi.yaml そのもの) で、
// そこに書いた servers (相対のプロキシ経路) は使わず、タブで選んだ環境のものにする
export function withServer(
  definition: string,
  server: { url: string; description: string },
): string {
  const spec = load(definition);
  if (typeof spec !== 'object' || spec === null || Array.isArray(spec)) {
    throw new Error('OpenAPI の定義がオブジェクトではない');
  }
  // JSON は YAML のうちなので、swagger-ui はそのまま読める
  return JSON.stringify({ ...spec, servers: [server] });
}

// Backstage のバックエンドのプロキシ (/api/proxy) の下の、環境の経路の URL。
// proxyPath は注釈 home-k8s/env.<環境>.api-proxy の値 (/sample-api-dev)
export function proxyServerUrl(proxyBaseUrl: string, proxyPath: string) {
  return `${proxyBaseUrl.replace(/\/+$/, '')}${proxyPath}`;
}
