// bff: 外からの唯一の入口 (APISIX の後ろ)。JWT を確かめ、api を Dapr の service invocation で呼ぶ。internal/bff
package main

import (
	"context"
	"net/http"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/auth"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/bff"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
)

func main() {
	server.Main("bff", func(ctx context.Context) error {
		issuer := server.Env("OIDC_ISSUER", "")
		s := &bff.Server{
			Verifier:   auth.NewOIDC(context.WithoutCancel(ctx), issuer, server.Env("OIDC_JWKS_URL", issuer+"/protocol/openid-connect/certs"), server.Env("OIDC_AUDIENCE", "gitops-deployer")),
			APIURL:     dapr.FromEnv().InvokeURL(server.Env("API_APP_ID", "api"), ""),
			HTTP:       &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport)},
			TemporalUI: server.Env("TEMPORAL_UI_URL", ""),
			TemporalNS: server.Env("TEMPORAL_NAMESPACE", ""),
			ArgoCD:     server.Env("ARGOCD_URL", ""),
			Timeout:    server.EnvDuration("REQUEST_TIMEOUT", 15*time.Second),
		}
		// InvokeURL の末尾の / と internal の先頭の / が重ならないように
		s.APIURL = s.APIURL[:len(s.APIURL)-1]
		health := &server.Health{}
		r := server.NewEngine("bff")
		health.Register(r)
		s.Register(r)
		health.SetReady(true)
		return server.Serve(ctx, ":"+server.Env("PORT", "8080"), r, health, 20*time.Second)
	})
}
