import { ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Tab, Tabs } from '@material-ui/core';
import { EmptyState } from '@backstage/core-components';
import { useEntity } from '@backstage/plugin-catalog-react';
import { Environment, environmentsWith } from './annotations';

// 環境を切り替えて中身を出す。選んだ環境は URL の ?env= に持つので、リンクやスクリーンショットで環境を指せる
export function EnvironmentSwitcher(props: {
  requires: readonly string[];
  children: (env: Environment) => ReactNode;
}) {
  const { entity } = useEntity();
  const [params, setParams] = useSearchParams();
  const envs = environmentsWith(entity, props.requires);
  if (envs.length === 0) {
    return (
      <EmptyState
        missing="info"
        title="環境の注釈がありません"
        description={`home-k8s/environments と home-k8s/env.<環境>.{${props.requires.join(
          ',',
        )}} を catalog-info.yaml に書く (docs/cluster/environments.md)`}
      />
    );
  }
  const selected = envs.find(e => e.name === params.get('env')) ?? envs[0];
  return (
    <>
      <Tabs
        value={selected.name}
        onChange={(_, name: string) => setParams({ env: name })}
        indicatorColor="primary"
      >
        {envs.map(e => (
          <Tab key={e.name} value={e.name} label={e.name} />
        ))}
      </Tabs>
      {/* key で環境ごとに作り直し、前の環境の読み込み結果を残さない */}
      <div key={selected.name} style={{ marginTop: 16 }}>
        {props.children(selected)}
      </div>
    </>
  );
}
