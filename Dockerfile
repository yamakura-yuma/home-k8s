FROM ubuntu:24.04

# docker: kind/kubectl/kubeadmが操作するDockerデーモンはホスト(WSL2)のものを
#   /var/run/docker.sock 経由でそのまま使う (Docker outside of Docker)。
#   このイメージにはdocker CLIのみ入れ、デーモンは同梱しない。
# nix: プロジェクト固有のツール一式 (just/kubectl/kind) はコンテナ内のNixから
#   `flake.nix` / `flake.lock` に基づいて導入する (`just devcontainer up` が実行する)。
#   コンテナにsystemdが無くrootで動かすため、Determinate Systems製の
#   nix-installerを --init none で使う (公式インストーラはroot実行を想定していない)。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install linux \
        --extra-conf "sandbox = false" \
        --init none \
        --no-confirm
ENV PATH="${PATH}:/nix/var/nix/profiles/default/bin"

WORKDIR /workspace
CMD ["sleep", "infinity"]
