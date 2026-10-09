import { Entity } from '@backstage/catalog-model';
import {
  ENV_KEYS,
  entityForEnvironment,
  environmentsWith,
  readEnvironments,
} from './annotations';

const entity: Entity = {
  apiVersion: 'backstage.io/v1alpha1',
  kind: 'Component',
  metadata: {
    name: 'sample-api',
    annotations: {
      'home-k8s/environments': 'dev, prod',
      'home-k8s/env.dev.api-proxy': '/sample-api-dev',
      'home-k8s/env.dev.grafana-host-id': 'dev',
      'home-k8s/env.prod.api-proxy': '/sample-api-prod',
      'grafana/host-id': 'default',
    },
  },
};

describe('readEnvironments', () => {
  it('並びの順に、環境ごとの値を集める', () => {
    expect(readEnvironments(entity)).toEqual([
      {
        name: 'dev',
        values: { 'api-proxy': '/sample-api-dev', 'grafana-host-id': 'dev' },
      },
      { name: 'prod', values: { 'api-proxy': '/sample-api-prod' } },
    ]);
  });

  it('home-k8s/environments が無ければ環境は無い', () => {
    expect(readEnvironments({ ...entity, metadata: { name: 'x' } })).toEqual(
      [],
    );
  });
});

describe('environmentsWith', () => {
  it('キーを全部持つ環境だけを返す', () => {
    expect(
      environmentsWith(entity, ['grafana-host-id']).map(e => e.name),
    ).toEqual(['dev']);
    expect(environmentsWith(entity, ['temporal-url'])).toEqual([]);
  });
});

describe('entityForEnvironment', () => {
  it('環境の値をプラグインの注釈に重ね、元のエンティティは変えない', () => {
    const [dev, prod] = readEnvironments(entity);
    const mapping = { 'grafana/host-id': 'grafana-host-id' };
    expect(
      entityForEnvironment(entity, dev, mapping).metadata.annotations?.[
        'grafana/host-id'
      ],
    ).toBe('dev');
    // 環境に値が無ければ元の注釈のまま
    expect(
      entityForEnvironment(entity, prod, mapping).metadata.annotations?.[
        'grafana/host-id'
      ],
    ).toBe('default');
    expect(entity.metadata.annotations?.['grafana/host-id']).toBe('default');
  });
});

describe('argocd-app-name', () => {
  const sample: Entity = {
    ...entity,
    metadata: {
      ...entity.metadata,
      annotations: {
        'home-k8s/environments': 'dev,prod',
        'home-k8s/env.dev.argocd-app-name': 'sample-api-dev',
        'home-k8s/env.prod.argocd-app-name': 'sample-api-prod',
      },
    },
  };

  it('環境ごとの Application の名前を、プラグインの注釈 argocd/app-name に重ねる', () => {
    const mapping = { 'argocd/app-name': ENV_KEYS.argocdAppName };
    const names = readEnvironments(sample).map(
      env =>
        entityForEnvironment(sample, env, mapping).metadata.annotations?.[
          'argocd/app-name'
        ],
    );
    expect(names).toEqual(['sample-api-dev', 'sample-api-prod']);
  });

  it('注釈を持たないエンティティ (home-k8s など) には ArgoCD のタブを出さない', () => {
    expect(environmentsWith(sample, [ENV_KEYS.argocdAppName])).toHaveLength(2);
    expect(environmentsWith(entity, [ENV_KEYS.argocdAppName])).toEqual([]);
  });
});
