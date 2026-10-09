import { Grid } from '@material-ui/core';
import { compatWrapper } from '@backstage/core-compat-api';
import { EntityProvider, useEntity } from '@backstage/plugin-catalog-react';
import {
  EntityArgoCDHistoryCard,
  EntityArgoCDOverviewCard,
} from '@roadiehq/backstage-plugin-argo-cd';
import { Environment, ENV_KEYS, entityForEnvironment } from './annotations';

// Roadie の ArgoCD プラグインの注釈 argocd/app-name は 1 つしか書けないので、選んだ環境の Application の名前
// (注釈 home-k8s/env.<環境>.argocd-app-name) を重ねたエンティティを渡し、プラグインの部品をそのまま出す。
// プラグインの部品は旧 API (createComponentExtension) なので compatWrapper で包む
export function ArgoCdView({ env }: { env: Environment }) {
  const { entity } = useEntity();
  const scoped = entityForEnvironment(entity, env, {
    'argocd/app-name': ENV_KEYS.argocdAppName,
  });
  return compatWrapper(
    <EntityProvider entity={scoped}>
      <Grid container spacing={3}>
        <Grid item xs={12}>
          <EntityArgoCDOverviewCard
            title={`${env.name} の同期状態と健全性`}
            subtitle={env.values[ENV_KEYS.argocdAppName]}
          />
        </Grid>
        <Grid item xs={12}>
          <EntityArgoCDHistoryCard />
        </Grid>
      </Grid>
    </EntityProvider>,
  );
}
