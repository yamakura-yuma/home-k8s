// 手元の docker で、Azure の Resource Graph の応答を偽物にするための preload (本番のイメージでは使わない)。
// 実サブスクリプションが無くても、カタログの取り込み (plugin-catalog-backend-module-azure-resources) を通しで確かめるためのもの。
//   docker run ... -v $PWD/dev:/fake:ro -e NODE_OPTIONS='--require /fake/fake-azure-resource-graph.js' ...
// ResourceGraphClient.resources() を、下の表に対して app-config.yaml の KQL と同じ絞り込み
// (tags.environment == <env>、tags.service == sample-api、extend environment = '<env>') をかけて返すものに差し替える。
// KQL を解釈するのではなく、その 3 つの条件を正規表現で拾うだけ。KQL そのものの確かめにはならない (Azure の実物でしか確かめられない)。
const { createRequire } = require('module');
const req = createRequire(process.cwd() + '/');
const { ResourceGraphClient } = req('@azure/arm-resourcegraph');

const sub = '00000000-0000-0000-0000-0000000000aa';
const id = (rg, type, name) =>
  `/subscriptions/${sub}/resourceGroups/${rg}/providers/${type}/${name}`;
const row = (rg, type, name, location, tags) => ({
  id: id(rg, type, name),
  name,
  type: type.toLowerCase(),
  tenantId: '00000000-0000-0000-0000-0000000000bb',
  location,
  resourceGroup: rg,
  subscriptionId: sub,
  tags,
});
const table = [
  row('rg-sample-dev', 'Microsoft.Web/sites', 'sample-api-dev', 'japaneast', { environment: 'dev', service: 'sample-api' }),
  row('rg-sample-dev', 'Microsoft.Storage/storageAccounts', 'sampleapidevstore', 'japaneast', { environment: 'Dev', service: 'sample-api' }),
  row('rg-sample-dev', 'Microsoft.KeyVault/vaults', 'sample-api-dev-kv', 'japaneast', { environment: 'dev', service: 'sample-api', 'backstage.io-owner': 'user:default/yamakura-yuma' }),
  row('rg-sample-prod', 'Microsoft.Web/sites', 'sample-api-prod', 'japaneast', { environment: 'prod', service: 'sample-api' }),
  row('rg-sample-prod', 'Microsoft.Sql/servers', 'sample-api-prod-sql', 'japaneast', { environment: 'prod', service: 'sample-api' }),
  // 絞り込みで落ちるはずのもの
  row('rg-other', 'Microsoft.Web/sites', 'other-app-dev', 'japaneast', { environment: 'dev', service: 'other' }),
  row('rg-sample-stg', 'Microsoft.Web/sites', 'sample-api-stg', 'japaneast', { environment: 'stg', service: 'sample-api' }),
  row('rg-none', 'Microsoft.Web/sites', 'untagged', 'japaneast', {}),
];

ResourceGraphClient.prototype.resources = async function (request) {
  const q = request.query;
  const env = /tolower\(tostring\(tags\['environment'\]\)\) == '([^']+)'/.exec(q)?.[1];
  const service = /tolower\(tostring\(tags\['service'\]\)\) == '([^']+)'/.exec(q)?.[1];
  const added = /extend environment = '([^']+)'/.exec(q)?.[1];
  const data = table
    .filter(r => (r.tags.environment ?? '').toLowerCase() === env && (r.tags.service ?? '').toLowerCase() === service)
    .map(r => ({ ...r, environment: added }));
  console.log(`[fake-resource-graph] subscriptions=${JSON.stringify(request.subscriptions)} env=${env} -> ${data.length} rows`);
  return { data, skipToken: undefined, count: data.length };
};
