// Package obs は 6 つの Pod に共通の観測の設定 (OTel のトレースと、trace_id 付きの構造化ログ)。
// トレースは OTEL_EXPORTER_OTLP_ENDPOINT (既存の OTel Collector、observability) へ OTLP/gRPC で送る。
// 伝播は W3C Trace Context (traceparent)。HTTP は otelhttp・otelgin、Temporal は interceptor、
// Dapr の pub/sub は CloudEvent の traceparent が同じ文脈を運ぶ。
package obs

import (
	"context"
	"io"
	"log/slog"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// Setup はトレースの送り先と slog の既定のハンドラを決める。返す関数は送り残しを流してから閉じる (SIGTERM の後に呼ぶ)。
func Setup(ctx context.Context, service string) (func(context.Context) error, error) {
	slog.SetDefault(slog.New(NewHandler(os.Stdout, service)))
	opts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(resource.NewSchemaless(
			attribute.String("service.name", service),
			attribute.String("service.namespace", "gitops-deployer"),
		)),
	}
	// 送り先が無い (単体試験・手元) ときは記録だけして送らない
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" {
		exp, err := otlptracegrpc.New(ctx)
		if err != nil {
			return nil, err
		}
		opts = append(opts, sdktrace.WithBatcher(exp))
	}
	tp := sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	return tp.Shutdown, nil
}

// Handler は JSON の slog ハンドラに、文脈のスパンの trace_id・span_id と service を足す (ログからトレースへ飛べる)。
type Handler struct{ slog.Handler }

func NewHandler(w io.Writer, service string) *Handler {
	return &Handler{slog.NewJSONHandler(w, nil).WithAttrs([]slog.Attr{slog.String("service", service)})}
}

func (h *Handler) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
	}
	return h.Handler.Handle(ctx, r)
}

func (h *Handler) WithAttrs(as []slog.Attr) slog.Handler { return &Handler{h.Handler.WithAttrs(as)} }
func (h *Handler) WithGroup(name string) slog.Handler    { return &Handler{h.Handler.WithGroup(name)} }

// TraceParent は文脈のスパンを traceparent の文字列にする (Workflow の memo に残し、event-receiver が同じ trace に繋ぐ)。
func TraceParent(ctx context.Context) string {
	c := propagation.MapCarrier{}
	propagation.TraceContext{}.Inject(ctx, c)
	return c["traceparent"]
}

// FromTraceParent は traceparent の文字列から親の文脈を作る。読めなければ ctx をそのまま返す。
func FromTraceParent(ctx context.Context, tp string) context.Context {
	if tp == "" {
		return ctx
	}
	return propagation.TraceContext{}.Extract(ctx, propagation.MapCarrier{"traceparent": tp})
}
