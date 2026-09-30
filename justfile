# ツールは開発用コンテナ (home-k8s-dev) の中にある。レシピの各行は just/dev-shell を通り、
# ホストで打たれたらコンテナの中で実行される (コンテナの中ならそのまま実行)。
# 1 行ごとに docker exec するので、コンテナに頼るレシピは shebang を使わず行で書くこと。
set shell := ["just/dev-shell", "-cu"]

default:
    #!/usr/bin/env bash
    just --list

import 'just/devcontainer.just'
import 'just/kind.just'
import 'just/observability.just'
