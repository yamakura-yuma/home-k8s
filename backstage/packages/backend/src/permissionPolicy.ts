// permission framework のポリシー。admins は何でもでき、それ以外 (viewers・127.0.0.1 と share のゲスト) は読むだけ。
//
// グループは Backstage のトークンの ownership (ent。userInfo サービスが credentials から引く) で見る。Keycloak でサインインした人は keycloakAuth.ts が
// group:default/admins・group:default/viewers を入れる。ゲスト (user:development/guest) はどちらも持たない。
// admins 以外に許すのは、action が read の permission と、何も書かない検査 2 つ (下の READ_ONLY_CHECKS) だけ。
// それ以外 (カタログの登録・削除・再読み込み、Azure の起動・停止など、action が create・update・delete のものと、
// action の無いもの) は拒む。知らない permission も拒む側に倒す。
import {
  coreServices,
  createBackendModule,
  UserInfoService,
} from '@backstage/backend-plugin-api';
import {
  AuthorizeResult,
  PolicyDecision,
} from '@backstage/plugin-permission-common';
import {
  PermissionPolicy,
  PolicyQuery,
  PolicyQueryUser,
} from '@backstage/plugin-permission-node';
import { policyExtensionPoint } from '@backstage/plugin-permission-node/alpha';

export const ADMINS_GROUP = 'group:default/admins';

// action を持たないが、何も書き換えない permission (catalog-common の定義)。
// validate はエンティティの YAML の検査、analyze は登録の画面が URL を登録する前に中身を読むだけ
const READ_ONLY_CHECKS = new Set(['catalog.entity.validate', 'catalog.location.analyze']);

export class AdminsWritePolicy implements PermissionPolicy {
  constructor(private readonly userInfo: UserInfoService) {}

  async handle(
    request: PolicyQuery,
    user?: PolicyQueryUser,
  ): Promise<PolicyDecision> {
    if (user) {
      const { ownershipEntityRefs } = await this.userInfo.getUserInfo(
        user.credentials,
      );
      if (ownershipEntityRefs.includes(ADMINS_GROUP)) {
        return { result: AuthorizeResult.ALLOW };
      }
    }
    const { permission } = request;
    if (
      permission.attributes.action === 'read' ||
      READ_ONLY_CHECKS.has(permission.name)
    ) {
      return { result: AuthorizeResult.ALLOW };
    }
    return { result: AuthorizeResult.DENY };
  }
}

export const permissionModuleAdminsWritePolicy = createBackendModule({
  pluginId: 'permission',
  moduleId: 'admins-write-policy',
  register(reg) {
    reg.registerInit({
      deps: { policy: policyExtensionPoint, userInfo: coreServices.userInfo },
      async init({ policy, userInfo }) {
        policy.setPolicy(new AdminsWritePolicy(userInfo));
      },
    });
  },
});
