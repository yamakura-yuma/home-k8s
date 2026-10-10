// サインインの画面と、Keycloak (realm home-k8s) の OIDC の API。
//
// tailnet の URL (*.ts.net) で開いたときは Keycloak だけを出す。ゲストの経路は tailnet の入口の proxy が拒むので、出しても入れない。
// それ以外 (127.0.0.1 の localhost:7007 と share Pod の公開 URL) ではゲストだけを出す。OIDC の callback は tailnet の URL にしか
// 戻らない (Keycloak の client backstage の redirect URI) ので、そこでは使えない。理由は docs/cluster/backstage.md の「ログインと権限」。
import {
  ApiBlueprint,
  BackstageIdentityApi,
  configApiRef,
  createApiRef,
  createFrontendModule,
  discoveryApiRef,
  oauthRequestApiRef,
  OpenIdConnectApi,
  ProfileInfoApi,
  SessionApi,
} from '@backstage/frontend-plugin-api';
import { SignInPageBlueprint } from '@backstage/plugin-app-react';
import { OAuth2 } from '@backstage/core-app-api';
import { SignInPage } from '@backstage/core-components';

export const keycloakAuthApiRef = createApiRef<
  OpenIdConnectApi & ProfileInfoApi & BackstageIdentityApi & SessionApi
>({ id: 'auth.keycloak' });

const keycloakProvider = {
  id: 'keycloak',
  title: 'Keycloak',
  message: 'home-k8s の Keycloak (realm home-k8s) でサインインする',
  apiRef: keycloakAuthApiRef,
};

// 開いたホスト名から、サインインの画面に出す provider を決める
export function signInProviders(hostname: string) {
  return hostname.endsWith('.ts.net')
    ? [keycloakProvider]
    : (['guest'] as const).slice();
}

const keycloakAuthApi = ApiBlueprint.make({
  name: 'keycloak',
  params: defineParams =>
    defineParams({
      api: keycloakAuthApiRef,
      deps: {
        discoveryApi: discoveryApiRef,
        oauthRequestApi: oauthRequestApiRef,
        configApi: configApiRef,
      },
      factory: ({ discoveryApi, oauthRequestApi, configApi }) =>
        OAuth2.create({
          configApi,
          discoveryApi,
          oauthRequestApi,
          // バックエンドの provider の id (packages/backend/src/keycloakAuth.ts)
          provider: { id: 'oidc', title: 'Keycloak', icon: () => null },
          environment: configApi.getOptionalString('auth.environment'),
          defaultScopes: ['openid', 'profile', 'email'],
        }),
    }),
});

const signInPage = SignInPageBlueprint.make({
  params: {
    loader: async () => props =>
      (
        <SignInPage
          {...props}
          auto
          providers={signInProviders(window.location.hostname)}
        />
      ),
  },
});

export const signInModule = createFrontendModule({
  pluginId: 'app',
  extensions: [keycloakAuthApi, signInPage],
});
