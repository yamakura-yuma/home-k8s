// api: Workflow の開始・照会と履歴 (Dapr state) の読み出し。外には出さず bff からだけ呼ばれる。internal/api
package main

import (
	"context"
	"strings"
	"time"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/api"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/history"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/temporalx"
)

func main() {
	server.Main("api", func(ctx context.Context) error {
		tc, err := temporalx.New()
		if err != nil {
			return err
		}
		defer tc.Close()
		health := &server.Health{}
		r := server.NewEngine("api")
		health.Register(r)
		r.Use(server.Deadline(server.EnvDuration("REQUEST_TIMEOUT", 15*time.Second)))
		(&api.Server{
			T:           tc,
			Store:       &history.Store{D: dapr.FromEnv()},
			Registries:  strings.Split(server.Env("IMAGE_REGISTRIES", "docker.io"), ","),
			WaitTimeout: server.EnvDuration("WAIT_TIMEOUT", 20*time.Minute),
		}).Register(r)
		go temporalx.WaitHealthy(ctx, tc, func() { health.SetReady(true) })
		return server.Serve(ctx, ":"+server.Env("PORT", "8080"), r, health, 20*time.Second)
	})
}
