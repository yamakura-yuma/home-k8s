#!/usr/bin/env bash
# orca-exporter の systemd ユーザーユニットを入れる / 消す / 状態を見る。
# `just orca-exporter install|uninstall|status` から呼ばれる。ホスト (WSL) で動かす。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
unit=orca-exporter.service
unit_file="$HOME/.config/systemd/user/$unit"
state_dir="$HOME/.local/share/home-k8s/observability/orca-exporter"

# orca CLI は Orca の relay を通るホストのラッパで、開発用コンテナの中には無い
if [ -n "${HOME_K8S_DEV:-}" ]; then
    echo "orca-exporter はホスト (WSL) で動かす。開発用コンテナの外で打つ" >&2
    exit 1
fi

case "$1" in
  install)
    orca_bin="$(command -v orca || true)"
    if [ -z "$orca_bin" ]; then
        echo "orca CLI が見つからない。Orca の端末 (PATH に ~/.orca-relay/bin がある) で打つ" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$unit_file")"
    # ~/.local/share/home-k8s/observability は開発用コンテナ (root) が作るので、ホストのユーザーは
    # 中に書けない。状態のディレクトリだけ、docker 経由で自分の持ち物として作る
    if ! mkdir -p "$state_dir" 2>/dev/null; then
        docker run --rm --entrypoint install -v "$(dirname "$state_dir")":/d home-k8s-dev \
            -d -o "$(id -u)" -g "$(id -g)" /d/"$(basename "$state_dir")"
    fi
    # ユニットはこの checkout の exporter を直接指す。worktree で入れたら main に戻して入れ直す
    sed -e "s|@EXPORTER@|$here/orca_exporter.py|" -e "s|@ORCA_BIN@|$orca_bin|" "$here/$unit" > "$unit_file"
    systemctl --user daemon-reload
    systemctl --user enable "$unit" >/dev/null
    systemctl --user restart "$unit"
    echo "入れた: $unit_file"
    systemctl --user --no-pager status "$unit" | head -5
    ;;
  uninstall)
    systemctl --user disable --now "$unit" 2>/dev/null || true
    rm -f "$unit_file"
    systemctl --user daemon-reload
    # 状態 (送信済みのキー) は残す。消すと入れ直したときに全部を送り直し、Loki と Tempo で二重になる
    echo "消した: $unit_file (状態 $state_dir は残した)"
    ;;
  status)
    systemctl --user --no-pager status "$unit" | head -4 || true
    echo "--- 直近のログ"
    journalctl --user -u "$unit" -n 5 --no-pager -o cat 2>/dev/null || true
    echo "--- 状態"
    python3 "$here/orca_exporter.py" --status
    ;;
  *)
    echo "usage: $0 <install|uninstall|status>" >&2
    exit 1
    ;;
esac
