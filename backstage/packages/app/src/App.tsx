import { createApp } from '@backstage/frontend-defaults';
import apiDocsPlugin from '@backstage/plugin-api-docs/alpha';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import argocdPlugin from '@roadiehq/backstage-plugin-argo-cd/alpha';
import grafanaPlugin from '@backstage-community/plugin-grafana/alpha';
import techdocsPlugin from '@backstage/plugin-techdocs/alpha';
import searchPlugin from '@backstage/plugin-search/alpha';
import scaffolderPlugin from '@backstage/plugin-scaffolder/alpha';
import { navModule } from './modules/nav';
import { environmentsPlugin } from './modules/environments';
import { azurePlugin } from './modules/azure';
import { signInModule } from './modules/signin';

export default createApp({
  features: [
    catalogPlugin,
    apiDocsPlugin,
    grafanaPlugin,
    argocdPlugin,
    techdocsPlugin,
    searchPlugin,
    // Create の画面 (/create)。gitops-deployer のデプロイ・削除のテンプレート (docs/cluster/gitops-deployer-backstage.md)
    scaffolderPlugin,
    environmentsPlugin,
    azurePlugin,
    navModule,
    signInModule,
  ],
});
