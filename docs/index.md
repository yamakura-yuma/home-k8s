# home-k8s の文書

kind クラスタで Kubernetes を学ぶ環境と、Claude Code・Orca の観測スタックの文書。
全体の概要と使い方はリポジトリの README にある。

- cluster: kind クラスタ・ArgoCD・Backstage・データの永続化
- observability: Claude Code・Orca のテレメトリと Grafana のダッシュボード
- share: Grafana・headroom・Backstage を Quick Tunnel で人に見せる share Pod。**[cluster/share.md](cluster/share.md) から読む**（構成・CLI・試験の正本）。コードとの対応は下の表
- certification: Kubernetes の認定試験の学習ロードマップ

## share のファイル対応表

share を直すときは、[cluster/share.md](cluster/share.md) の該当する節を読んでから下のファイルを開く。

| 見たいこと | ファイル | share.md の節 |
|---|---|---|
| `just share add / delete / list / get / rotate / prune` の本体 | [just/share.sh](https://github.com/yamakura-yuma/home-k8s/blob/main/just/share.sh)、レシピは [just/share.just](https://github.com/yamakura-yuma/home-k8s/blob/main/just/share.just) | CLI |
| Quick Tunnel の URL を引く | [just/share-urls.sh](https://github.com/yamakura-yuma/home-k8s/blob/main/just/share-urls.sh) | 1 つの資格情報で 3 対象 |
| `just up` で作る Secret | [just/share-secrets.sh](https://github.com/yamakura-yuma/home-k8s/blob/main/just/share-secrets.sh) | manifest・ArgoCD |
| headroom のホスト側の中継 | [just/share-relay.sh](https://github.com/yamakura-yuma/home-k8s/blob/main/just/share-relay.sh)、[just/share-relay.Caddyfile](https://github.com/yamakura-yuma/home-k8s/blob/main/just/share-relay.Caddyfile) | 届き方 |
| 経路の許可リスト・デフォルト拒否 | [clusters/kind/share/Caddyfile](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/Caddyfile) | 既存の経路の絞り込み |
| 要求ごとの認証・期限切れの判定 | [clusters/kind/share/share_auth.py](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/share_auth.py) | 期限切れ・削除の強制 |
| Pod（caddy・auth・cloudflared×3） | [clusters/kind/share/deployment.yaml](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/deployment.yaml)、[kustomization.yaml](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/kustomization.yaml) | manifest・ArgoCD |
| auth が読める Secret の絞り込み | [clusters/kind/share/rbac.yaml](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/rbac.yaml) | manifest・ArgoCD |
| 試験 | [test_share_auth.py](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/test_share_auth.py)、[test_share_caddy.py](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/test_share_caddy.py)、[test_share_manifest.py](https://github.com/yamakura-yuma/home-k8s/blob/main/clusters/kind/share/test_share_manifest.py)、[just/test_share_cli.py](https://github.com/yamakura-yuma/home-k8s/blob/main/just/test_share_cli.py)、[just/test_share_relay.py](https://github.com/yamakura-yuma/home-k8s/blob/main/just/test_share_relay.py)、[just/test_share_secrets.py](https://github.com/yamakura-yuma/home-k8s/blob/main/just/test_share_secrets.py) | 試験 |
