import useAsync from 'react-use/lib/useAsync';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
} from '@material-ui/core';
import {
  ANNOTATION_VIEW_URL,
  Entity,
  stringifyEntityRef,
} from '@backstage/catalog-model';
import { useApi } from '@backstage/core-plugin-api';
import { InfoCard, Link, Progress } from '@backstage/core-components';
import { catalogApiRef, EntityRefLink } from '@backstage/plugin-catalog-react';
import { Environment } from '../environments/annotations';

// カタログモジュール (plugin-catalog-backend-module-azure-resources) が取り込んだ Resource の注釈 (既定の対応づけ)
const LOCATION_ANNOTATION = 'management.azure.com/location';

// 取り込まれた Resource は、namespace が環境の名前で、spec.dependencyOf でこの Component に結ばれている
// (app-config.yaml の catalog.providers.azureResources)。選んだ環境の Resource を依存先として一覧にする
export function AzureResourcesList({
  entity,
  env,
}: {
  entity: Entity;
  env: Environment;
}) {
  const catalogApi = useApi(catalogApiRef);
  const ref = stringifyEntityRef(entity);
  const { value, loading, error } = useAsync(async () => {
    const { items } = await catalogApi.getEntities({
      filter: {
        kind: 'Resource',
        'metadata.namespace': env.name,
        'relations.dependencyOf': ref,
      },
    });
    return items;
  }, [catalogApi, env.name, ref]);

  return (
    <InfoCard
      title={`${env.name} の Azure のリソース (Resource Graph から取り込み)`}
    >
      {loading && <Progress />}
      {error && <p>カタログを読めません: {error.message}</p>}
      {value && value.length === 0 && (
        <p>
          取り込まれたリソースは無い (Azure のリソースに タグ environment=
          {env.name}・service=
          {entity.metadata.name}{' '}
          を付けると、次の取り込みで出る。docs/cluster/backstage-azure.md)
        </p>
      )}
      {value && value.length > 0 && (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>名前</TableCell>
              <TableCell>種類</TableCell>
              <TableCell>場所</TableCell>
              <TableCell>Azure</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {value.map(resource => {
              const portal =
                resource.metadata.annotations?.[ANNOTATION_VIEW_URL];
              return (
                <TableRow key={stringifyEntityRef(resource)}>
                  <TableCell>
                    <EntityRefLink
                      entityRef={resource}
                      title={resource.metadata.title ?? resource.metadata.name}
                    />
                  </TableCell>
                  <TableCell>{String(resource.spec?.type ?? '')}</TableCell>
                  <TableCell>
                    {resource.metadata.annotations?.[LOCATION_ANNOTATION] ?? ''}
                  </TableCell>
                  <TableCell>
                    {portal ? <Link to={portal}>ポータル</Link> : ''}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </InfoCard>
  );
}
