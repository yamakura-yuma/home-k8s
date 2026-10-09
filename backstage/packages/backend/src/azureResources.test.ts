import { readFileSync } from 'fs';
import { resolve } from 'path';
import { load } from 'js-yaml';
import { ConfigReader } from '@backstage/config';
import { azureResourcesConfigured } from './azureResources';

// app-config.yaml のうち、Azure のリソースの取り込みの設定。${AZURE_*} は環境変数が入った体で置き換える
const appConfig = load(
  readFileSync(resolve(__dirname, '../../../app-config.yaml'), 'utf8'),
) as any;
const providers: any[] = appConfig.catalog.providers.azureResources;

function configWith(
  credentials: Record<string, string> | undefined,
  subscription?: string,
) {
  return new ConfigReader({
    azureResources: credentials ? { credentials } : undefined,
    catalog: {
      providers: {
        azureResources: providers.map(p => ({
          ...p,
          // ${AZURE_SUBSCRIPTION_ID} が無いと、設定の読み込みが要素ごと消して空の配列にする
          scope: { subscriptions: subscription ? [subscription] : [] },
        })),
      },
    },
  });
}

const credentials = {
  tenantId: '00000000-0000-0000-0000-000000000000',
  clientId: '11111111-1111-1111-1111-111111111111',
  clientSecret: 'secret',
};

describe('azureResourcesConfigured', () => {
  it('資格情報が揃い、プロバイダーに subscription があれば載せる', () => {
    expect(azureResourcesConfigured(configWith(credentials, 'sub'))).toBe(true);
  });

  it('資格情報が無ければ載せない', () => {
    expect(azureResourcesConfigured(configWith(undefined, 'sub'))).toBe(false);
  });

  it.each(Object.keys(credentials))('%s が欠けたら載せない', key => {
    const { [key]: _removed, ...rest } = credentials as Record<string, string>;
    expect(azureResourcesConfigured(configWith(rest, 'sub'))).toBe(false);
  });

  it('subscription が無い (AZURE_SUBSCRIPTION_ID が無い) なら載せない', () => {
    expect(azureResourcesConfigured(configWith(credentials))).toBe(false);
  });

  it('プロバイダーが 1 つも無ければ載せない', () => {
    expect(
      azureResourcesConfigured(
        new ConfigReader({ azureResources: { credentials } }),
      ),
    ).toBe(false);
  });
});

describe('app-config.yaml のプロバイダー', () => {
  it('dev・prod ごとに 1 つで、KQL が環境のタグとサービスのタグで絞る', () => {
    expect(providers.map(p => p.id)).toEqual([
      'sample-api-dev',
      'sample-api-prod',
    ]);
    for (const [provider, env] of [
      [providers[0], 'dev'],
      [providers[1], 'prod'],
    ] as const) {
      expect(provider.query).toContain(
        `tolower(tostring(tags['environment'])) == '${env}'`,
      );
      expect(provider.query).toContain(
        `tolower(tostring(tags['service'])) == 'sample-api'`,
      );
      // namespace は KQL で足す列 environment から取るので、列の値が環境の名前でなければならない
      expect(provider.query).toContain(`extend environment = '${env}'`);
    }
  });

  it('Resource は namespace が環境で、spec.dependencyOf が sample-api の Component になる', () => {
    for (const provider of providers) {
      expect(provider.mapping.metadata.namespace).toBe('environment');
      expect(provider.mapping.spec.dependencyOf).toEqual([
        'component:default/sample-api',
      ]);
      // namespace が default でないので、owner は省略せず default と書く (省略すると dev/user:... になり、解決できない)
      expect(provider.defaultOwner).toBe('user:default/yamakura-yuma');
    }
  });

  it('資格情報は azureSites と同じ環境変数から取る', () => {
    expect(appConfig.azureResources.credentials).toEqual({
      tenantId: '${AZURE_TENANT_ID}',
      clientId: '${AZURE_CLIENT_ID}',
      clientSecret: '${AZURE_CLIENT_SECRET}',
    });
    for (const provider of providers) {
      expect(provider.scope.subscriptions).toEqual([
        '${AZURE_SUBSCRIPTION_ID}',
      ]);
    }
  });
});
