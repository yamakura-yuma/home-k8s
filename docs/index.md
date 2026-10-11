# home-k8s の文書

kind クラスタで Kubernetes を学ぶ環境と、Claude Code・Orca の観測スタックの文書。
全体の概要と使い方はリポジトリの README にある。

- cluster: kind クラスタ・ArgoCD・Backstage・データの永続化。ゲスト (他の人) の入れ方の比較は [cluster/guest-access.md](cluster/guest-access.md)
- observability: Claude Code・Orca のテレメトリと Grafana のダッシュボード
- share (廃止): Grafana・headroom・Backstage を Quick Tunnel で人に見せていた share Pod。2026-10-11 に廃止した。設計の記録は [cluster/share.md](cluster/share.md)、ゲストの入れ方は [cluster/guest-access.md](cluster/guest-access.md)
- certification: Kubernetes の認定試験の学習ロードマップ

