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
          kind
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
      });
}
