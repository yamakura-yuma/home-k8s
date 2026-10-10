// Keycloak (realm home-k8s) の OIDC でのサインイン。provider の id は oidc (callback は /api/auth/oidc/handler/frame)。
//
// sign-in resolver はカタログを引かない。権限の正本は Keycloak のグループで、userinfo の groups クレーム
// (admins・viewers) をそのまま Backstage のトークンの ownership (ent) に group:default/<グループ> として入れる。
// permission のポリシー (permissionPolicy.ts) はこの ent を見る。カタログの Group admins・viewers は表示用の静的な宣言
// (catalog-info.yaml) で、メンバーは持たない。どちらのグループにも入っていない人はサインインさせない (Grafana の
// role_attribute_strict と同じ扱い)。理由は docs/cluster/backstage.md の「ログインと権限」。
import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  DEFAULT_NAMESPACE,
  stringifyEntityRef,
} from '@backstage/catalog-model';
import {
  authProvidersExtensionPoint,
  createOAuthProviderFactory,
  OAuthAuthenticatorResult,
  SignInInfo,
  AuthResolverContext,
} from '@backstage/plugin-auth-node';
import {
  oidcAuthenticator,
  OidcAuthResult,
} from '@backstage/plugin-auth-backend-module-oidc-provider';

// 権限を分けるグループ。Keycloak の realm home-k8s のグループの名前と同じ
export const ROLE_GROUPS = ['admins', 'viewers'] as const;

// カタログのエンティティの名前に使える形 (catalog-model の既定の検査と同じ)
const ENTITY_NAME = /^([A-Za-z0-9][-_.A-Za-z0-9]*)?[A-Za-z0-9]$/;

export function keycloakIdentity(userinfo: Record<string, unknown>): {
  userEntityRef: string;
  ownershipEntityRefs: string[];
} {
  const name = userinfo.preferred_username;
  if (typeof name !== 'string' || name.length > 63 || !ENTITY_NAME.test(name)) {
    throw new Error(
      'Keycloak のユーザー名 (preferred_username) がカタログの名前に使えない',
    );
  }
  const groups = Array.isArray(userinfo.groups) ? userinfo.groups : [];
  const roleGroups = ROLE_GROUPS.filter(g => groups.includes(g));
  if (roleGroups.length === 0) {
    throw new Error(
      'Keycloak のグループ admins・viewers のどちらにも入っていないので、サインインできない',
    );
  }
  const userEntityRef = stringifyEntityRef({
    kind: 'User',
    namespace: DEFAULT_NAMESPACE,
    name,
  });
  return {
    userEntityRef,
    ownershipEntityRefs: [
      userEntityRef,
      ...roleGroups.map(g =>
        stringifyEntityRef({ kind: 'Group', namespace: DEFAULT_NAMESPACE, name: g }),
      ),
    ],
  };
}

export async function keycloakSignInResolver(
  info: SignInInfo<OAuthAuthenticatorResult<OidcAuthResult>>,
  ctx: AuthResolverContext,
) {
  const { userEntityRef, ownershipEntityRefs } = keycloakIdentity(
    info.result.fullProfile.userinfo,
  );
  return ctx.issueToken({
    claims: { sub: userEntityRef, ent: ownershipEntityRefs },
  });
}

export const authModuleKeycloakOidc = createBackendModule({
  pluginId: 'auth',
  moduleId: 'keycloak-oidc',
  register(reg) {
    reg.registerInit({
      deps: { providers: authProvidersExtensionPoint },
      async init({ providers }) {
        providers.registerProvider({
          providerId: 'oidc',
          factory: createOAuthProviderFactory({
            authenticator: oidcAuthenticator,
            signInResolver: keycloakSignInResolver,
          }),
        });
      },
    });
  },
});
