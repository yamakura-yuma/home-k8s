// Azure のタブ (azure-sites プラグイン) のバックエンドを、資格情報があるときだけ載せる。
//
// 公式のバックエンド (@backstage-community/plugin-azure-sites-backend) は、起動時に app-config.yaml の
// azureSites.domain・tenantId を getString で読み、無いと init で落ちる。バックエンドは 1 つのプロセスなので、
// 資格情報の無い今 (just up が Git の外のファイルから Secret を作るまで) それを載せると Backstage ごと起動しない。
// そこで azureSites の資格情報が揃っているときだけ公式のプラグインを載せ、揃っていないときは同じ pluginId
// (azure-sites) の代役を載せる。代役は /health に 503 を返すだけで、フロントエンドのタブはそれを見て
// 「資格情報が無い」と出す (packages/app/src/modules/azure)。
// 資格情報は clusters/kind/backstage/values.yaml が Secret backstage-azure (任意) から環境変数 AZURE_* にし、
// app-config.yaml の azureSites が ${AZURE_*} で受ける。環境変数が無いと、その項目は設定から消える。
import {
  coreServices,
  createBackendFeatureLoader,
  createBackendPlugin,
} from '@backstage/backend-plugin-api';
import { Config } from '@backstage/config';

// 公式のプラグインが読む項目のうち、無いと落ちるもの (domain・tenantId) と、サービスプリンシパルでの認証に要るもの
// (clientId・clientSecret)。ひとつでも欠ければ「資格情報が無い」とする (DefaultAzureCredential は使わない)
const REQUIRED_KEYS = ['domain', 'tenantId', 'clientId', 'clientSecret'];

export function azureSitesConfigured(config: Config): boolean {
  return REQUIRED_KEYS.every(key => config.has(`azureSites.${key}`));
}

// 資格情報が無いときの代役。フロントエンドが見分けられるよう、公式のプラグインと同じ pluginId と /health にする
export const azureSitesNotConfigured = createBackendPlugin({
  pluginId: 'azure-sites',
  register(env) {
    env.registerInit({
      deps: {
        httpRouter: coreServices.httpRouter,
        logger: coreServices.logger,
      },
      async init({ httpRouter, logger }) {
        logger.info(
          'azureSites の資格情報が無いので、Azure のタブは「資格情報が無い」を出す (docs/cluster/backstage-azure.md)',
        );
        httpRouter.use((_req, res) => {
          res.status(503).json({ status: 'not-configured' });
        });
        httpRouter.addAuthPolicy({ path: '/health', allow: 'unauthenticated' });
      },
    });
  },
});

export const azureSitesFeatureLoader = createBackendFeatureLoader({
  deps: { config: coreServices.rootConfig },
  *loader({ config }) {
    if (azureSitesConfigured(config)) {
      yield import('@backstage-community/plugin-azure-sites-backend');
    } else {
      yield azureSitesNotConfigured;
    }
  },
});
