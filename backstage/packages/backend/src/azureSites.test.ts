import { ConfigReader } from '@backstage/config';
import { azureSitesConfigured } from './azureSites';

const full = {
  domain: 'example.onmicrosoft.com',
  tenantId: '00000000-0000-0000-0000-000000000000',
  clientId: '11111111-1111-1111-1111-111111111111',
  clientSecret: 'secret',
};

describe('azureSitesConfigured', () => {
  it('domain・tenantId・clientId・clientSecret が揃っていれば資格情報がある', () => {
    expect(azureSitesConfigured(new ConfigReader({ azureSites: full }))).toBe(
      true,
    );
  });

  it('subscriptions の id が空 (環境変数が無い) なら資格情報が無い', () => {
    expect(
      azureSitesConfigured(
        new ConfigReader({ azureSites: { ...full, subscriptions: [{}] } }),
      ),
    ).toBe(false);
    expect(
      azureSitesConfigured(
        new ConfigReader({
          azureSites: { ...full, subscriptions: [{ id: 'sub' }] },
        }),
      ),
    ).toBe(true);
  });

  it('azureSites が無ければ資格情報が無い', () => {
    expect(azureSitesConfigured(new ConfigReader({}))).toBe(false);
  });

  it.each(Object.keys(full))('%s が欠けたら資格情報が無い', key => {
    const { [key]: _removed, ...rest } = full as Record<string, string>;
    expect(
      azureSitesConfigured(new ConfigReader({ azureSites: rest })),
    ).toBe(false);
  });
});
