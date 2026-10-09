// Azure のリソースをカタログの Resource として取り込むモジュール
// (@backstage-community/plugin-catalog-backend-module-azure-resources) を、資格情報があるときだけ載せる。
//
// このモジュールは載せるとかならずプロバイダー (catalog.providers.azureResources) を読んで定期実行に入る。
// 資格情報の無い今 (just up が Git の外のファイルから Secret を作るまで) に載せると、認証が DefaultAzureCredential に
// 落ち、取り込みのたびに失敗のログを出し続ける。そこで azureSites.ts と同じく、資格情報が揃っているときだけ import する
// (揃っていなければ何も載せない。カタログの他の読み込みには関係しない)。
// 資格情報は azure-sites と同じ AZURE_* の環境変数で、app-config.yaml の azureResources.credentials と
// catalog.providers.azureResources[].scope.subscriptions が ${AZURE_*} で受ける。環境変数が無い項目は設定から消える。
import {
  coreServices,
  createBackendFeatureLoader,
} from '@backstage/backend-plugin-api';
import { Config } from '@backstage/config';

// azureResourcesServiceRef (plugin-azure-resources-node) が ClientSecretCredential に渡す 3 つ。
// ひとつでも欠けると DefaultAzureCredential に落ちるので、「資格情報が無い」とする
const CREDENTIAL_KEYS = ['tenantId', 'clientId', 'clientSecret'];

export function azureResourcesConfigured(config: Config): boolean {
  const providers =
    config.getOptionalConfigArray('catalog.providers.azureResources') ?? [];
  return (
    CREDENTIAL_KEYS.every(key =>
      config.has(`azureResources.credentials.${key}`),
    ) &&
    providers.length > 0 &&
    // ${AZURE_SUBSCRIPTION_ID} が無いと scope.subscriptions が空の配列になり、プロバイダーが「scope が無い」で落ちる
    providers.every(
      provider =>
        (provider.getOptionalStringArray('scope.subscriptions') ?? []).length >
        0,
    )
  );
}

export const azureResourcesFeatureLoader = createBackendFeatureLoader({
  deps: { config: coreServices.rootConfig },
  *loader({ config }) {
    if (azureResourcesConfigured(config)) {
      yield import(
        '@backstage-community/plugin-catalog-backend-module-azure-resources'
      );
    }
  },
});
