// event-receiver: Knative Service (0↔1)。Kafka Broker の Trigger から Application の変化の CloudEvent を受け、
// Workflow に Signal を送る。internal/receiver
package main

import (
	"context"
	"time"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/receiver"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/temporalx"
)

func main() {
	server.Main("event-receiver", func(ctx context.Context) error {
		tc, err := temporalx.New()
		if err != nil {
			return err
		}
		defer tc.Close()
		health := &server.Health{}
		r := server.NewEngine("event-receiver")
		health.Register(r)
		(&receiver.Handler{T: tc}).Register(r)
		// 0 から起きたときは最初のイベントを待たせたくないので、Temporal を待たずに受ける (届かなければ 503 で Broker が打ち直す)
		health.SetReady(true)
		return server.Serve(ctx, ":"+server.Env("PORT", "8080"), r, health, 10*time.Second)
	})
}
