#!/usr/bin/env bash
# just show grafana の本体。引数: <viewer のパスワードファイル> <状態ファイル>
# just share が動いていれば公開 URL を、動いていなければ localhost:3000 を表示する。
# 表示するのは共有相手に渡す viewer の資格情報。admin のものは just show grafana-admin で見る。
# 状態ファイルは just share が公開中だけ置く。強制終了で残っていても、
# 書かれた pid の cloudflared が生きていなければ停止中として扱う。
set -euo pipefail
password_file="$1"
state="$2"

if [ ! -s "$password_file" ]; then
    echo "$password_file が無い。先に just up を打つ" >&2
    exit 1
fi

url=
if [ -s "$state" ]; then
    url=$(sed -n 's/^url=//p' "$state")
    pid=$(sed -n 's/^cloudflared_pid=//p' "$state")
    if [ -z "$pid" ] || ! grep -qs cloudflared "/proc/$pid/cmdline"; then
        url=
    fi
fi

if [ -n "$url" ]; then
    echo "Grafana は公開中 (just share)"
    echo "  URL:        $url"
else
    echo "Grafana は公開していない (share は停止中)"
    echo "  URL:        http://localhost:3000"
fi
cat <<EOF2
  ユーザー:   viewer
  パスワード: $(cat "$password_file")
EOF2
