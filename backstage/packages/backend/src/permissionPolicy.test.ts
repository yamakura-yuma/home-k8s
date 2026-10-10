import {
  catalogEntityCreatePermission,
  catalogEntityDeletePermission,
  catalogEntityReadPermission,
  catalogEntityRefreshPermission,
  catalogEntityValidatePermission,
  catalogLocationAnalyzePermission,
  catalogLocationCreatePermission,
  catalogLocationDeletePermission,
} from '@backstage/plugin-catalog-common/alpha';
import {
  AuthorizeResult,
  createPermission,
  Permission,
} from '@backstage/plugin-permission-common';
import {
  BackstageCredentials,
  BackstageUserPrincipal,
} from '@backstage/backend-plugin-api';
import { PolicyQueryUser } from '@backstage/plugin-permission-node';
import { AdminsWritePolicy } from './permissionPolicy';

// ownership はポリシーが userInfo サービスから引く。試験では credentials の userEntityRef ごとの値を返す偽物にする
const ownership: Record<string, string[]> = {};
const userInfo = {
  getUserInfo: async (credentials: BackstageCredentials<BackstageUserPrincipal>) => {
    const { userEntityRef } = credentials.principal;
    return { userEntityRef, ownershipEntityRefs: ownership[userEntityRef] };
  },
};

function user(...ownershipEntityRefs: string[]): PolicyQueryUser {
  const [userEntityRef] = ownershipEntityRefs;
  ownership[userEntityRef] = ownershipEntityRefs;
  return {
    credentials: {
      $$type: '@backstage/BackstageCredentials',
      principal: { type: 'user', userEntityRef },
    },
    info: { userEntityRef, ownershipEntityRefs: [userEntityRef] },
  };
}

const admin = user('user:default/test-admin', 'group:default/admins');
const viewer = user('user:default/test-viewer', 'group:default/viewers');
const guest = user('user:development/guest');

const writes: Permission[] = [
  catalogEntityCreatePermission,
  catalogEntityDeletePermission,
  catalogEntityRefreshPermission,
  catalogLocationCreatePermission,
  catalogLocationDeletePermission,
  createPermission({ name: 'azure.sites.update', attributes: { action: 'update' } }),
  // action の無い、知らない permission も拒む
  createPermission({ name: 'scaffolder.action.execute', attributes: {} }),
];
const reads: Permission[] = [
  catalogEntityReadPermission,
  catalogEntityValidatePermission,
  catalogLocationAnalyzePermission,
];

async function decide(permission: Permission, u?: PolicyQueryUser) {
  return (await new AdminsWritePolicy(userInfo).handle({ permission }, u))
    .result;
}

describe('AdminsWritePolicy', () => {
  it.each(writes.map(p => [p.name, p]))('admins は %s ができる', async (_, p) => {
    expect(await decide(p, admin)).toBe(AuthorizeResult.ALLOW);
  });

  it.each(writes.map(p => [p.name, p]))(
    'viewers・ゲスト・ユーザーなしは %s を拒まれる',
    async (_, p) => {
      expect(await decide(p, viewer)).toBe(AuthorizeResult.DENY);
      expect(await decide(p, guest)).toBe(AuthorizeResult.DENY);
      expect(await decide(p, undefined)).toBe(AuthorizeResult.DENY);
    },
  );

  it.each(reads.map(p => [p.name, p]))(
    'viewers・ゲストも %s はできる (読むだけ)',
    async (_, p) => {
      expect(await decide(p, viewer)).toBe(AuthorizeResult.ALLOW);
      expect(await decide(p, guest)).toBe(AuthorizeResult.ALLOW);
    },
  );
});
