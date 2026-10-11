// Package temporalx は Temporal (temporal-dev) への接続。OTel の tracing interceptor を入れ、client→Workflow→Activity と
// Signal の文脈を Temporal のヘッダで運ぶ (6 つの Pod のトレースが 1 本の trace になる)。
package temporalx

import (
	"context"
	"log/slog"
	"os"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/contrib/opentelemetry"
	"go.temporal.io/sdk/interceptor"
	tlog "go.temporal.io/sdk/log"
)

// New は TEMPORAL_ADDRESS・TEMPORAL_NAMESPACE の Temporal のクライアントを作る。繋ぐのは最初に使うとき (起動の順序に頼らない)。
//
// 住所は dev の temporal-internal-frontend:7236 (認可を通らない内部用の frontend、ClusterIP)。7233 の frontend は
// Keycloak の JWT を要求するが、合意書の Pod の構成で Temporal を使う Pod は資格情報を持たない。docs/cluster/gitops-deployer.md
func New() (client.Client, error) {
	tracer, err := opentelemetry.NewTracingInterceptor(opentelemetry.TracerOptions{})
	if err != nil {
		return nil, err
	}
	return client.NewLazyClient(client.Options{
		HostPort:     os.Getenv("TEMPORAL_ADDRESS"),
		Namespace:    os.Getenv("TEMPORAL_NAMESPACE"),
		Logger:       tlog.NewStructuredLogger(slog.Default()),
		Interceptors: []interceptor.ClientInterceptor{tracer},
	})
}

// WaitHealthy は Temporal が応答するまで待ち、応答したら ready を呼ぶ (readiness probe を上げる)。
func WaitHealthy(ctx context.Context, c client.Client, ready func()) {
	for {
		hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, err := c.CheckHealth(hctx, &client.CheckHealthRequest{})
		cancel()
		if err == nil {
			ready()
			return
		}
		slog.WarnContext(ctx, "Temporal が応答しない。待つ", "error", err.Error(), "address", os.Getenv("TEMPORAL_ADDRESS"))
		select {
		case <-ctx.Done():
			return
		case <-time.After(3 * time.Second):
		}
	}
}
