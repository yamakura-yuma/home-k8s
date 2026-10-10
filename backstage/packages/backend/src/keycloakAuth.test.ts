import { keycloakIdentity } from './keycloakAuth';

describe('keycloakIdentity', () => {
  it('Keycloak のグループを group:default/<グループ> として ownership に入れる', () => {
    expect(
      keycloakIdentity({ preferred_username: 'test-admin', groups: ['admins'] }),
    ).toEqual({
      userEntityRef: 'user:default/test-admin',
      ownershipEntityRefs: ['user:default/test-admin', 'group:default/admins'],
    });
    expect(
      keycloakIdentity({ preferred_username: 'test-viewer', groups: ['viewers'] })
        .ownershipEntityRefs,
    ).toEqual(['user:default/test-viewer', 'group:default/viewers']);
  });

  it('admins・viewers 以外のグループは入れない', () => {
    expect(
      keycloakIdentity({
        preferred_username: 'yamakura-yuma',
        groups: ['admins', 'other'],
      }).ownershipEntityRefs,
    ).toEqual(['user:default/yamakura-yuma', 'group:default/admins']);
  });

  it.each([[undefined], [[]], [['other']], ['admins']])(
    'admins・viewers のどちらにも入っていなければサインインさせない (groups=%j)',
    groups => {
      expect(() =>
        keycloakIdentity({ preferred_username: 'test-nogroup', groups }),
      ).toThrow('admins・viewers のどちらにも入っていない');
    },
  );

  it.each([[undefined], [''], ['a/b'], ['-x'], ['x'.repeat(64)]])(
    'カタログの名前に使えないユーザー名は断る (%j)',
    name => {
      expect(() =>
        keycloakIdentity({ preferred_username: name, groups: ['admins'] }),
      ).toThrow('preferred_username');
    },
  );
});
