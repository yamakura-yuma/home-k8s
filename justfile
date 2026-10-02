# ツールは開発用コンテナ (home-k8s-dev) の中にある。レシピの各行は just/dev-shell を通り、
# ホストで打たれたらコンテナの中で実行される (コンテナの中ならそのまま実行)。
# 1 行ごとに docker exec するので、コンテナに頼るレシピは shebang を使わず行で書くこと。
set shell := ["just/dev-shell", "-cu"]

# 操作する kind クラスタの context。検証用の別クラスタに向けるときだけ環境変数で変える
kube_context := env_var_or_default("HOME_K8S_KUBE_CONTEXT", "kind-study-kind")

default:
    #!/usr/bin/env bash
    just --list

import 'just/argocd.just'
import 'just/devcontainer.just'
import 'just/kind.just'
import 'just/observability.just'
import 'just/orca-exporter.just'
