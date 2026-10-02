import { createApp } from '@backstage/frontend-defaults';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import grafanaPlugin from '@backstage-community/plugin-grafana/alpha';
import techdocsPlugin from '@backstage/plugin-techdocs/alpha';
import { navModule } from './modules/nav';

export default createApp({
  features: [catalogPlugin, grafanaPlugin, techdocsPlugin, navModule],
});
