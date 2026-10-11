// Package server は 6 つの Pod に共通の起動と停止。SIGTERM を受けたら readiness を落とし、受け付け中の処理を終えてから
// (http.Server.Shutdown・worker.Stop) トレースを流して止まる。Pod の preStop の sleep の間に Service から外れる。
package server

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/contrib/instrumentation/github.com/gin-gonic/gin/otelgin"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/obs"
)

// Main は観測を設定し、run を SIGTERM・SIGINT で止まる ctx で呼ぶ。run が返したら OTel を流して終わる。
func Main(service string, run func(ctx context.Context) error) {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	shutdown, err := obs.Setup(ctx, service)
	if err != nil {
		slog.Error("観測の設定に失敗", "error", err.Error())
		os.Exit(1)
	}
	err = run(ctx)
	flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = shutdown(flushCtx)
	if err != nil {
		slog.Error("止まった", "error", err.Error())
		os.Exit(1)
	}
	slog.Info("止まった")
}

// Health は /healthz (liveness: プロセスが応答する) と /readyz (readiness: 依存に繋がり、止まりかけていない)。
type Health struct{ ready atomic.Bool }

func (h *Health) SetReady(v bool) { h.ready.Store(v) }

func (h *Health) Register(r gin.IRoutes) {
	r.GET("/healthz", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	r.GET("/readyz", func(c *gin.Context) {
		if !h.ready.Load() {
			c.String(http.StatusServiceUnavailable, "not ready")
			return
		}
		c.String(http.StatusOK, "ready")
	})
}

// NewEngine は gin に otelgin (受けた traceparent を親にしたスパン) とアクセスログ (trace_id 付き) を付ける。
func NewEngine(service string) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery(), otelgin.Middleware(service, otelgin.WithFilter(func(r *http.Request) bool {
		return r.URL.Path != "/healthz" && r.URL.Path != "/readyz"
	})), accessLog)
	return r
}

func accessLog(c *gin.Context) {
	start := time.Now()
	c.Next()
	if p := c.Request.URL.Path; p == "/healthz" || p == "/readyz" {
		return
	}
	slog.InfoContext(c.Request.Context(), "http",
		"method", c.Request.Method, "path", c.Request.URL.Path, "status", c.Writer.Status(),
		"duration_ms", time.Since(start).Milliseconds())
}

// Serve は ctx が終わるまで h を返し、終わったら readiness を落として受け付け中の要求を待つ (最大 grace)。
func Serve(ctx context.Context, addr string, h http.Handler, health *Health, grace time.Duration) error {
	srv := &http.Server{Addr: addr, Handler: h, ReadHeaderTimeout: 10 * time.Second}
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	if health != nil {
		health.SetReady(false)
	}
	slog.Info("SIGTERM を受けた。受け付け中の要求を待って止まる")
	sctx, cancel := context.WithTimeout(context.Background(), grace)
	defer cancel()
	if err := srv.Shutdown(sctx); err != nil {
		return err
	}
	if err := <-errc; !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Env は環境変数 (ConfigMap gitops-deployer から入る)。無ければ def。
func Env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func EnvDuration(key string, def time.Duration) time.Duration {
	if d, err := time.ParseDuration(os.Getenv(key)); err == nil {
		return d
	}
	return def
}

func EnvInt(key string, def int) int {
	if n, err := strconv.Atoi(os.Getenv(key)); err == nil {
		return n
	}
	return def
}

// DeadlineHeader は呼び出しの期限 (RFC 3339) を次の Pod へ渡すヘッダ。受けた側は自分の期限をこれより前にする。
const DeadlineHeader = "X-Request-Deadline"

// Deadline は要求の ctx に、上限 max とヘッダの期限の早いほうを付ける (deadline の伝播)。
func Deadline(max time.Duration) gin.HandlerFunc {
	return func(c *gin.Context) {
		deadline := time.Now().Add(max)
		if t, err := time.Parse(time.RFC3339Nano, c.GetHeader(DeadlineHeader)); err == nil && t.Before(deadline) {
			deadline = t
		}
		ctx, cancel := context.WithDeadline(c.Request.Context(), deadline)
		defer cancel()
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	}
}
