# just share を常駐させる構成 (docs/cluster/share.md) のうち、ホスト側の headroom 中継。公開レシピの `just up`・`just down` (justfile) が呼ぶ内部用。
# 中継は headroom (localhost:8787) の GET の許可リストだけを、kind の bridge のゲートウェイで待ち受けて転送する caddy コンテナ。
# 状態の確認は docker を素手で叩くこと (`docker ps --filter name=home-k8s-share-relay`)。

# 中継の共有トークン。リポジトリの外 (WSL2 ホスト側) に置き、2 回目以降は再利用する。値は Secret share/share-host にも入る
share_relay_token_file := env_var("HOME") + "/.local/share/home-k8s/share/relay-token"
# 中継が bridge のゲートウェイで待ち受けるポート。Pod へは Secret share-host の SHARE_RELAY_ADDR (ゲートウェイ:このポート) で渡る
share_relay_port := "8788"

# kind の bridge ができた後 (`just up` の _kind-up の後) に呼ぶ。トークンと Secret share-host を (無ければ作って) 用意し、中継のコンテナを作り直す
_share-relay-up:
    bash just/share-relay.sh up "{{share_relay_token_file}}" "{{justfile_directory()}}" {{kube_context}} {{share_relay_port}}

# 中継のコンテナを消す。トークンのファイルは残す
_share-relay-down:
    bash just/share-relay.sh down
