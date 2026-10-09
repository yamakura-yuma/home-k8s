import { azureSitesPlugin } from '@backstage-community/plugin-azure-sites';
import { convertLegacyPlugin } from '@backstage/core-compat-api';
import { ENV_KEYS } from '../environments/annotations';
import { createEnvironmentContent } from '../environments';
import { AzureSitesView } from './AzureSitesView';

// Azure のタブ。@backstage-community/plugin-azure-sites には新しいフロントエンドシステムの入口 (/alpha) が無いので、
// convertLegacyPlugin で API (azureSiteApiRef) を新しいシステムの拡張にして包み、部品は compatWrapper で出す。
// タブは dev・prod を切り替え、環境ごとの azure-web-sites をプラグインの注釈に重ねる (拡張 entity-content:azureSites/azure)
const azureContent = createEnvironmentContent({
  name: 'azure',
  path: '/azure',
  title: 'Azure',
  requires: [ENV_KEYS.azureWebSites],
  render: env => <AzureSitesView env={env} />,
});

export const azurePlugin = convertLegacyPlugin(azureSitesPlugin, {
  extensions: [azureContent],
});
