import { keycloakAuthApiRef, signInProviders } from '.';

describe('signInProviders', () => {
  it('tailnet の URL では Keycloak だけを出す (ゲストは出さない)', () => {
    const providers = signInProviders('backstage.taild2b611.ts.net');
    expect(providers).toHaveLength(1);
    expect(providers[0]).toMatchObject({ id: 'keycloak', apiRef: keycloakAuthApiRef });
  });

  it.each(['localhost', '127.0.0.1', 'example.trycloudflare.com'])(
    '%s ではゲストだけを出す (OIDC の callback は tailnet の URL にしか戻らない)',
    hostname => {
      expect(signInProviders(hostname)).toEqual(['guest']);
    },
  );

  it('ts.net で終わらない、ts.net を含むだけのホストはゲスト', () => {
    expect(signInProviders('backstage.ts.net.example.com')).toEqual(['guest']);
  });
});
