import { Entity } from '@backstage/catalog-model';

// 環境 (kind の中の namespace dev・prod) ごとの接続先をエンティティの注釈で持つ規約 (docs/cluster/environments.md)。
//   home-k8s/environments: "dev,prod"            環境の並び (タブの切り替えの順)。namespace の名前と同じ
//   home-k8s/env.<環境>.<キー>: <値>               環境ごとの値。キーは下の ENV_KEYS
export const ENVIRONMENTS_ANNOTATION = 'home-k8s/environments';
export const ENV_ANNOTATION_PREFIX = 'home-k8s/env.';

// 環境ごとの値のキー。後続の PR はここに足し、docs/cluster/environments.md の表も直す
export const ENV_KEYS = {
  // Backstage のバックエンドのプロキシの経路 (/api/proxy の下)。サービスの API を読む
  apiProxy: 'api-proxy',
  // 環境の Application の名前 (ArgoCD)。Roadie の ArgoCD プラグインの注釈 argocd/app-name に重ねる
  argocdAppName: 'argocd-app-name',
  // Azure の App Service・Functions の名前 (部分一致、大文字小文字を問わない)。azure-sites プラグインの注釈 azure.com/microsoft-web-sites に重ねる
  azureWebSites: 'azure-web-sites',
  // ブラウザが開く Temporal Web UI の URL (iframe の src)。環境ごとのホストのポート (docs/cluster/temporal.md)
  temporalUrl: 'temporal-url',
} as const;

export type Environment = {
  name: string;
  // 注釈 home-k8s/env.<name>.<キー> の値を、キーで引けるようにしたもの
  values: Record<string, string>;
};

export function envAnnotation(env: string, key: string): string {
  return `${ENV_ANNOTATION_PREFIX}${env}.${key}`;
}

export function readEnvironments(entity: Entity): Environment[] {
  const annotations = entity.metadata.annotations ?? {};
  const names = (annotations[ENVIRONMENTS_ANNOTATION] ?? '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
  return names.map(name => {
    const prefix = `${ENV_ANNOTATION_PREFIX}${name}.`;
    const values: Record<string, string> = {};
    for (const [key, value] of Object.entries(annotations)) {
      if (key.startsWith(prefix)) values[key.slice(prefix.length)] = value;
    }
    return { name, values };
  });
}

// 環境の値をすべて持つ環境だけを返す。タブを出すかどうかと、切り替えに並べる環境に使う
export function environmentsWith(
  entity: Entity,
  keys: readonly string[],
): Environment[] {
  return readEnvironments(entity).filter(env =>
    keys.every(key => env.values[key]),
  );
}

// 1 つのインスタンス前提のプラグイン (Grafana の grafana/host-id、ArgoCD の argocd/app-name など) に環境を渡すため、
// 環境の値をプラグインの注釈に重ねたエンティティを作る。mapping は { プラグインの注釈: 環境のキー }。
// タブはこのエンティティを EntityProvider で渡し、プラグインの部品をそのまま使う
export function entityForEnvironment(
  entity: Entity,
  env: Environment,
  mapping: Record<string, string>,
): Entity {
  const overlay: Record<string, string> = {};
  for (const [annotation, key] of Object.entries(mapping)) {
    const value = env.values[key];
    if (value) overlay[annotation] = value;
  }
  return {
    ...entity,
    metadata: {
      ...entity.metadata,
      annotations: { ...entity.metadata.annotations, ...overlay },
    },
  };
}
