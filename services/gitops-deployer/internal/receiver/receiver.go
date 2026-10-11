// Package receiver は event-receiver (Knative Service) の本体。Kafka Broker の Trigger から CloudEvent を受け、
// ArgoCD の Application の変化を、そのアプリの Workflow (app-<名前>) への Signal にする。
//
//   - CloudEvent の data は Application そのもの (ApiServerSource の mode Resource)。アプリの名前は ApplicationSet が付ける
//     ラベル gitops-deployer.home-k8s/app から読む
//   - 走っている Workflow が無ければ (処理の外の変化) 受け取ったことにして捨てる (2xx。Broker は打ち直さない)
//   - Temporal に届かなければ 5xx。Broker が打ち直し、尽きたら Dead Letter (Trigger の無い Broker dead-letter) へ送る
//   - 重複 (at-least-once) は Workflow が CloudEvent の id で捨てる
//   - トレース: Workflow の memo の traceparent を親にし、受けた CloudEvent の traceparent をリンクにする。
//     Signal は Temporal の interceptor が同じ文脈で運ぶ
package receiver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/converter"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/obs"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

// LabelApp は ApplicationSet gitops-deployer-apps が Application に付けるラベル (値はアプリの名前)。
const LabelApp = "gitops-deployer.home-k8s/app"

const typeDelete = "dev.knative.apiserver.resource.delete"

type Temporal interface {
	SignalWorkflow(ctx context.Context, workflowID, runID, signalName string, arg any) error
	DescribeWorkflowExecution(ctx context.Context, workflowID, runID string) (*workflowservice.DescribeWorkflowExecutionResponse, error)
}

type Handler struct{ T Temporal }

type application struct {
	Metadata struct {
		Name   string            `json:"name"`
		Labels map[string]string `json:"labels"`
	} `json:"metadata"`
	Status struct {
		Sync struct {
			Status   string `json:"status"`
			Revision string `json:"revision"`
		} `json:"sync"`
		Health struct {
			Status string `json:"status"`
		} `json:"health"`
	} `json:"status"`
}

// Parse は CloudEvent (binary か structured) を ArgoEvent にする。アプリのものでなければ ok=false。
func Parse(r *http.Request, body []byte) (ev workflow.ArgoEvent, ok bool, err error) {
	id, typ := r.Header.Get("Ce-Id"), r.Header.Get("Ce-Type")
	data := body
	if strings.HasPrefix(r.Header.Get("Content-Type"), "application/cloudevents+json") {
		var ce struct {
			ID   string          `json:"id"`
			Type string          `json:"type"`
			Data json.RawMessage `json:"data"`
		}
		if err := json.Unmarshal(body, &ce); err != nil {
			return ev, false, err
		}
		id, typ, data = ce.ID, ce.Type, ce.Data
	}
	if id == "" || typ == "" {
		return ev, false, errors.New("CloudEvent の id か type が無い")
	}
	var app application
	if err := json.Unmarshal(data, &app); err != nil {
		return ev, false, err
	}
	name := app.Metadata.Labels[LabelApp]
	if spec.ValidateName(name) != nil {
		return ev, false, nil
	}
	return workflow.ArgoEvent{
		ID: id, Type: typ, App: name, Exists: typ != typeDelete,
		Sync: app.Status.Sync.Status, Health: app.Status.Health.Status, Revision: app.Status.Sync.Revision,
	}, true, nil
}

func (h *Handler) Register(r gin.IRoutes) { r.POST("/", h.receive) }

func (h *Handler) receive(c *gin.Context) {
	body, err := io.ReadAll(io.LimitReader(c.Request.Body, 4<<20))
	if err != nil {
		c.Status(http.StatusBadRequest)
		return
	}
	ev, ok, err := Parse(c.Request, body)
	if err != nil {
		// 読めないイベントは打ち直しても読めない。受け取ったことにして捨てる (Broker を詰まらせない)
		slog.WarnContext(c.Request.Context(), "読めない CloudEvent を捨てた", "error", err.Error())
		c.JSON(http.StatusOK, gin.H{"ignored": err.Error()})
		return
	}
	if !ok {
		c.JSON(http.StatusOK, gin.H{"ignored": "gitops-deployer のアプリではない"})
		return
	}
	status, msg := h.signal(c.Request.Context(), ev)
	c.JSON(status, gin.H{"result": msg, "workflowId": workflow.ID(ev.App)})
}

func (h *Handler) signal(ctx context.Context, ev workflow.ArgoEvent) (int, string) {
	wfID := workflow.ID(ev.App)
	log := slog.With("app", ev.App, "ce_id", ev.ID, "ce_type", ev.Type, "sync", ev.Sync, "health", ev.Health, "revision", ev.Revision)
	desc, err := h.T.DescribeWorkflowExecution(ctx, wfID, "")
	var nf *serviceerror.NotFound
	if errors.As(err, &nf) {
		log.InfoContext(ctx, "Workflow が無いので捨てた")
		return http.StatusOK, "no workflow"
	}
	if err != nil {
		log.ErrorContext(ctx, "Temporal に届かない", "error", err.Error())
		return http.StatusServiceUnavailable, err.Error()
	}
	info := desc.GetWorkflowExecutionInfo()
	if info.GetStatus() != enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING {
		log.InfoContext(ctx, "Workflow が走っていないので捨てた", "status", info.GetStatus().String())
		return http.StatusOK, "workflow not running"
	}
	// 開始した要求の trace に繋ぐ (memo の traceparent が親、受けた CloudEvent の文脈はリンク)
	var tp string
	if p := info.GetMemo().GetFields()[workflow.MemoTraceParent]; p != nil {
		_ = converter.GetDefaultDataConverter().FromPayload(p, &tp)
	}
	parent := obs.FromTraceParent(context.Background(), tp)
	sctx, span := otel.Tracer("event-receiver").Start(parent, "signal "+workflow.SignalArgo,
		trace.WithLinks(trace.LinkFromContext(ctx)),
		trace.WithAttributes(attribute.String("cloudevents.event_id", ev.ID), attribute.String("cloudevents.event_type", ev.Type),
			attribute.String("gitops_deployer.app", ev.App), attribute.String("argocd.sync", ev.Sync), attribute.String("argocd.health", ev.Health)))
	defer span.End()
	sctx, cancel := context.WithCancel(sctx)
	defer cancel()
	go func() {
		// 受けた要求の期限 (Broker の timeout) で止める
		select {
		case <-ctx.Done():
			cancel()
		case <-sctx.Done():
		}
	}()
	err = h.T.SignalWorkflow(sctx, wfID, info.GetExecution().GetRunId(), workflow.SignalArgo, ev)
	if errors.As(err, &nf) {
		log.InfoContext(sctx, "Signal の前に Workflow が終わった")
		return http.StatusOK, "workflow finished"
	}
	if err != nil {
		span.RecordError(err)
		log.ErrorContext(sctx, "Signal を送れない", "error", err.Error())
		return http.StatusServiceUnavailable, err.Error()
	}
	log.InfoContext(sctx, "Signal を送った", "workflow_id", wfID)
	return http.StatusAccepted, "signaled"
}
