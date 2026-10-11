// store-activity: Task Queue store-activity の Activity (履歴を Dapr state に残し、完了イベントを Dapr pub/sub に流す)。internal/history
package main

import (
	"context"
	"time"

	"go.temporal.io/sdk/worker"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/history"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/temporalx"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

func main() {
	server.Main("store-activity", func(ctx context.Context) error {
		tc, err := temporalx.New()
		if err != nil {
			return err
		}
		defer tc.Close()
		w := worker.New(tc, workflow.StoreTaskQueue, worker.Options{WorkerStopTimeout: 20 * time.Second})
		(&history.Activities{Store: &history.Store{D: dapr.FromEnv()}}).Register(w)
		return server.RunWorker(ctx, "store-activity", w)
	})
}
