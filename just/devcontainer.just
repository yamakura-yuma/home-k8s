# 開発用コンテナ (Dockerfile) のライフサイクル操作。
# `docker` はホスト(WSL2)側に必要 (kindクラスタ自体もこれを前提にしている)。
#
# `just devcontainer up` / `just devcontainer shell` / `just devcontainer down`
# の形式で呼び出す。
devcontainer action:
    #!/usr/bin/env bash
    set -euo pipefail
    name=home-k8s-dev
    root="{{justfile_directory()}}"
    # git worktreeの場合、.gitはメインリポジトリ側の絶対パスを参照しているため、
    # コンテナ内でもホストと同じ絶対パスで見えるようにマウントする
    # (パスを付け替えるとgit/nixがgitdirを解決できなくなる)。
    main_repo="$(dirname "$(realpath "$(git -C "$root" rev-parse --git-common-dir)")")"
    case "{{action}}" in
      up)
        mkdir -p "$HOME/.kube"
        docker build -t "$name" "$root"
        if docker ps -aq -f "name=^${name}$" | grep -q .; then
          docker start "$name" >/dev/null
        else
          docker run -d --name "$name" \
            --network host \
            -e HOME="$HOME" \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "$root":"$root" \
            -v "$main_repo":"$main_repo" \
            -v "$HOME/.kube":"$HOME/.kube" \
            -w "$root" \
            "$name"
        fi
        # コンテナはrootで動くため、マウントしたリポジトリ (別ユーザー所有) を
        # gitがそのまま扱えるようsafe.directoryを設定してから導入する。
        docker exec -w "$root" "$name" bash -lc "
          git config --global --add safe.directory '$root' &&
          git config --global --add safe.directory '$main_repo' &&
          nix profile install .
        "
        ;;
      shell)
        docker exec -it -w "$root" "$name" bash -l
        ;;
      down)
        docker rm -f "$name" 2>/dev/null || echo "コンテナは起動していません"
        ;;
      *)
        echo "usage: just devcontainer <up|shell|down>" >&2
        exit 1
        ;;
    esac
