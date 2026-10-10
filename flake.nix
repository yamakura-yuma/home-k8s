{
  description = "home-k8s: Kubernetes資格学習用クラスタ構築環境の依存ツール";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        tools = with pkgs; [
          just
          kubectl
          # kubectl の OIDC のログイン (kubectl oidc-login、exec plugin)。docs/cluster/kube-oidc.md
          kubelogin-oidc
          kind
          kubernetes-helm
          cloudflared
          caddy
        ];
      in
      {
        # devcontainer の postCreateCommand から `nix profile install .` で導入する
        packages.default = pkgs.buildEnv {
          name = "home-k8s-tools";
          paths = tools;
        };

        # `nix develop` でも同じツール一式をシェルに持ち込める
        devShells.default = pkgs.mkShell {
          packages = tools;
        };

        # `just ci` (just/ci.sh) が使う静的チェックのツール。`nix develop .#ci -c` で入る。
        # 開発用コンテナには入れない (`nix profile install .` は packages.default だけ)。
        devShells.ci = pkgs.mkShell {
          packages = with pkgs; [
            yamllint
            yq-go
            kubernetes-helm
            kustomize
            kubeconform
            kube-linter
            python3
            caddy # Caddyfile の validate と、経路の統合試験 (just/ci.sh)
            nodejs_24 # backstage の jest と tsc (just/ci.sh)。Dockerfile の node:24 と揃える。engines は 22 || 24
            yarn-berry # backstage/.yarnrc.yml の yarnPath (.yarn/releases) の 4.13.0 に委ねる起動役
          ];
        };
      });
}
