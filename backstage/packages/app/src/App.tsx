import { createApp } from '@backstage/frontend-defaults';
import apiDocsPlugin from '@backstage/plugin-api-docs/alpha';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import grafanaPlugin from '@backstage-community/plugin-grafana/alpha';
import techdocsPlugin from '@backstage/plugin-techdocs/alpha';
import searchPlugin from '@backstage/plugin-search/alpha';
import { navModule } from './modules/nav';
import { environmentsPlugin } from './modules/environments';

export default createApp({
  features: [
    catalogPlugin,
    apiDocsPlugin,
    grafanaPlugin,
    techdocsPlugin,
    searchPlugin,
    environmentsPlugin,
    navModule,
  ],
});
