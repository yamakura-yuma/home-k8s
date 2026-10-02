# Backstage (home-k8s)

home-k8s の kind クラスタで動かす Backstage のアプリ。`npx @backstage/create-app@0.9.2` (Backstage 1.55.0、
新しいフロントエンドシステム) の雛形から、カタログと Grafana プラグイン
(`@backstage-community/plugin-grafana`) 以外を外したもの。

- イメージは `just up` が `Dockerfile` (上流の multi-stage build) で作り、kind load する。ホストに node・yarn は要らない
- 設定は `app-config.yaml`。読み込む `catalog-info.yaml` は chart の values (`clusters/kind/backstage/values.yaml`) で渡す
- 構成と使い方は [docs/cluster/backstage.md](../docs/cluster/backstage.md)
