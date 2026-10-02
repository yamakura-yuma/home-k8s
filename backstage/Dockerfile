# Backstage のイメージ。上流の multi-stage build (https://backstage.io/docs/deployment/docker#multi-stage-build)
# を元にし、ビルドもこの中で行う (ホストに node と yarn を要らないようにする)。
# 開発用コンテナの docker には buildx が無く BuildKit が使えないので、上流の RUN --mount=type=cache は外した。
# `just up` が開発用コンテナから build し、kind load でクラスタに入れる (just/backstage.just)。

# Stage 1 - yarn install の骨組み (package.json だけ) を作る
FROM node:24-trixie-slim AS packages

WORKDIR /app
COPY backstage.json package.json yarn.lock ./
COPY .yarn ./.yarn
COPY .yarnrc.yml ./
COPY packages packages

RUN find packages \! -name "package.json" -mindepth 2 -maxdepth 2 -exec rm -rf {} \+

# Stage 2 - 依存を入れて build する
FROM node:24-trixie-slim AS build

# better-sqlite3 (DB はインメモリの SQLite) のビルドに要る
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 g++ build-essential libsqlite3-dev && \
    rm -rf /var/lib/apt/lists/*

# BuildKit でない docker build では WORKDIR が root の持ち物で作られるので、先に node に渡す
RUN mkdir /app && chown node:node /app
USER node
WORKDIR /app

COPY --from=packages --chown=node:node /app .

RUN yarn install --immutable

COPY --chown=node:node . .

RUN yarn tsc
RUN yarn --cwd packages/backend build

RUN mkdir packages/backend/dist/skeleton packages/backend/dist/bundle \
    && tar xzf packages/backend/dist/skeleton.tar.gz -C packages/backend/dist/skeleton \
    && tar xzf packages/backend/dist/bundle.tar.gz -C packages/backend/dist/bundle

# Stage 3 - 実行用のイメージ。production の依存だけを入れる
FROM node:24-trixie-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 g++ build-essential libsqlite3-dev && \
    rm -rf /var/lib/apt/lists/*

# BuildKit でない docker build では WORKDIR が root の持ち物で作られるので、先に node に渡す
RUN mkdir /app && chown node:node /app
USER node
WORKDIR /app

COPY --from=build --chown=node:node /app/.yarn ./.yarn
COPY --from=build --chown=node:node /app/.yarnrc.yml  ./
COPY --from=build --chown=node:node /app/backstage.json ./
COPY --from=build --chown=node:node /app/yarn.lock /app/package.json /app/packages/backend/dist/skeleton/ ./

RUN yarn workspaces focus --all --production && rm -rf "$(yarn cache clean)"

COPY --from=build --chown=node:node /app/packages/backend/dist/bundle/ ./
COPY --chown=node:node app-config.yaml ./

ENV NODE_ENV=production

CMD ["node", "packages/backend", "--config", "app-config.yaml"]
