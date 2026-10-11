# gitops-deployer のイメージ (1 つ)。6 つの Pod は同じイメージで、command の /usr/local/bin/<名前> (cmd/<名前>) で起動し分ける。
# testkube の E2E (e2e/) も同じイメージに入れる。`just up` が build して kind load する (just/gitops-deployer.just)。レジストリには push しない。
# 開発用コンテナの docker には buildx が無く BuildKit が使えないので、RUN --mount は使わない。
FROM golang:1.26.7-alpine AS build
WORKDIR /src
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -trimpath -ldflags="-s -w" -o /out/ ./cmd/... ./e2e

# CA 証明書 (GitHub・Keycloak の HTTPS) を持ち、シェルも root も無い
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/ /usr/local/bin/
USER 65532:65532
