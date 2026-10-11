// workflow-engine: Workflow Engine Pod。Task Queue workflow-engine で Workflow (internal/workflow) だけを実行する。
// 外部 I/O を持たず、手順・待機・タイムアウト・Signal・補償の進行を一元管理する。
package main

import (
	"context"
	"time"

	"go.temporal.io/sdk/worker"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/temporalx"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

func main() {
	server.Main("workflow-engine", func(ctx context.Context) error {
		tc, err := temporalx.New()
		if err != nil {
			return err
		}
		defer tc.Close()
		w := worker.New(tc, workflow.TaskQueue, worker.Options{WorkerStopTimeout: 20 * time.Second})
		w.RegisterWorkflowWithOptions(workflow.Deploy, workflow.RegisterOptions())
		return server.RunWorker(ctx, "workflow-engine", w)
	})
}
