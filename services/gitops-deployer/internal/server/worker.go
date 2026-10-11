package server

import (
	"context"
	"time"

	"go.temporal.io/sdk/worker"
)

// RunWorker は Temporal の Worker を動かし、ctx が終わったら (SIGTERM) 新しいタスクの取得をやめ、実行中の Activity を
// 最大 WorkerStopTimeout 待ってから止まる。取りこぼしたタスクは Temporal が別の Worker (作り直された Pod) に渡し直す。
// probe 用に :8080 で /healthz・/readyz を返す (Worker が動いている間だけ ready)。
func RunWorker(ctx context.Context, service string, w worker.Worker) error {
	health := &Health{}
	r := NewEngine(service)
	health.Register(r)
	errc := make(chan error, 1)
	go func() { errc <- Serve(ctx, ":"+Env("PORT", "8080"), r, health, 5*time.Second) }()
	if err := w.Start(); err != nil {
		return err
	}
	health.SetReady(true)
	select {
	case <-ctx.Done():
	case err := <-errc:
		w.Stop()
		return err
	}
	health.SetReady(false)
	w.Stop()
	return <-errc
}
