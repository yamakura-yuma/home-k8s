package receiver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	commonpb "go.temporal.io/api/common/v1"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	workflowpb "go.temporal.io/api/workflow/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/converter"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

type fakeTemporal struct {
	status    enumspb.WorkflowExecutionStatus
	missing   bool
	signalErr error
	signals   []workflow.ArgoEvent
}

func (f *fakeTemporal) DescribeWorkflowExecution(_ context.Context, id, _ string) (*workflowservice.DescribeWorkflowExecutionResponse, error) {
	if f.missing {
		return nil, serviceerror.NewNotFound("no " + id)
	}
	tp, _ := converter.GetDefaultDataConverter().ToPayload("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
	return &workflowservice.DescribeWorkflowExecutionResponse{WorkflowExecutionInfo: &workflowpb.WorkflowExecutionInfo{
		Execution: &commonpb.WorkflowExecution{WorkflowId: id, RunId: "run1"},
		Status:    f.status,
		Memo:      &commonpb.Memo{Fields: map[string]*commonpb.Payload{workflow.MemoTraceParent: tp}},
	}}, nil
}

func (f *fakeTemporal) SignalWorkflow(_ context.Context, _, _, _ string, arg any) error {
	if f.signalErr != nil {
		return f.signalErr
	}
	f.signals = append(f.signals, arg.(workflow.ArgoEvent))
	return nil
}

const appJSON = `{"metadata":{"name":"deploy-hello","labels":{"gitops-deployer.home-k8s/app":"hello"}},
 "status":{"sync":{"status":"Synced","revision":"abc"},"health":{"status":"Healthy"}}}`

func post(h *Handler, headers map[string]string, body string) *httptest.ResponseRecorder {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h.Register(r)
	req := httptest.NewRequest("POST", "/", strings.NewReader(body))
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

var binary = map[string]string{"Ce-Id": "e1", "Ce-Type": "dev.knative.apiserver.resource.update", "Ce-Source": "https://kubernetes.default.svc", "Ce-Specversion": "1.0", "Content-Type": "application/json"}

func TestSignalsTheRunningWorkflow(t *testing.T) {
	f := &fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING}
	w := post(&Handler{T: f}, binary, appJSON)
	if w.Code != http.StatusAccepted || len(f.signals) != 1 {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	ev := f.signals[0]
	if ev.ID != "e1" || ev.App != "hello" || !ev.Exists || ev.Sync != "Synced" || ev.Health != "Healthy" || ev.Revision != "abc" {
		t.Errorf("%+v", ev)
	}
}

func TestStructuredDeleteEvent(t *testing.T) {
	f := &fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING}
	body := `{"specversion":"1.0","id":"e2","type":"dev.knative.apiserver.resource.delete","source":"s","data":` + appJSON + `}`
	w := post(&Handler{T: f}, map[string]string{"Content-Type": "application/cloudevents+json"}, body)
	if w.Code != http.StatusAccepted || len(f.signals) != 1 || f.signals[0].Exists {
		t.Fatalf("%d %s %+v", w.Code, w.Body, f.signals)
	}
}

func TestIgnoresWhatIsNotOurs(t *testing.T) {
	cases := map[string]struct {
		f    *fakeTemporal
		body string
	}{
		"ラベルの無い Application": {&fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING}, `{"metadata":{"name":"keycloak"}}`},
		"ラベルの値が不正":          {&fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING}, `{"metadata":{"labels":{"gitops-deployer.home-k8s/app":"../x"}}}`},
		"読めない data":         {&fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING}, `not json`},
		"Workflow が無い":       {&fakeTemporal{missing: true}, appJSON},
		"Workflow が終わっている":   {&fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_COMPLETED}, appJSON},
	}
	for name, c := range cases {
		w := post(&Handler{T: c.f}, binary, c.body)
		if w.Code != http.StatusOK || len(c.f.signals) != 0 {
			t.Errorf("%s: 2xx で捨てるはず: %d %s", name, w.Code, w.Body)
		}
	}
}

func TestTemporalFailureIsRetriedByTheBroker(t *testing.T) {
	f := &fakeTemporal{status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING, signalErr: serviceerror.NewUnavailable("down")}
	if w := post(&Handler{T: f}, binary, appJSON); w.Code != http.StatusServiceUnavailable {
		t.Errorf("5xx で Broker に打ち直させる: %d", w.Code)
	}
}
