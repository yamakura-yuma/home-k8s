package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	commonpb "go.temporal.io/api/common/v1"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	workflowpb "go.temporal.io/api/workflow/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/mocks"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr/daprtest"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/history"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

// fakeTemporal は Workflow ID ごとに 1 つだけ走らせる (ConflictPolicy FAIL の振る舞い)。
type fakeTemporal struct {
	running map[string]workflow.Input
	starts  []client.StartWorkflowOptions
}

func (f *fakeTemporal) ExecuteWorkflow(_ context.Context, o client.StartWorkflowOptions, _ any, args ...any) (client.WorkflowRun, error) {
	if _, ok := f.running[o.ID]; ok {
		return nil, serviceerror.NewWorkflowExecutionAlreadyStarted("running", "", "run0")
	}
	f.running[o.ID] = args[0].(workflow.Input)
	f.starts = append(f.starts, o)
	run := &mocks.WorkflowRun{}
	run.On("GetRunID").Return("run" + string(rune('0'+len(f.starts))))
	return run, nil
}

func (f *fakeTemporal) DescribeWorkflowExecution(_ context.Context, id, _ string) (*workflowservice.DescribeWorkflowExecutionResponse, error) {
	if _, ok := f.running[id]; !ok {
		return nil, serviceerror.NewNotFound("")
	}
	return &workflowservice.DescribeWorkflowExecutionResponse{WorkflowExecutionInfo: &workflowpb.WorkflowExecutionInfo{
		Execution: &commonpb.WorkflowExecution{WorkflowId: id, RunId: "run1"}, Status: enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING}}, nil
}

func (f *fakeTemporal) QueryWorkflow(context.Context, string, string, string, ...any) (converter.EncodedValue, error) {
	return encoded{workflow.Status{Phase: workflow.PhaseWaitingArgo, PR: &workflow.PR{Number: 9}}}, nil
}

// encoded は Query の結果 (converter.EncodedValue) の偽物。JSON を通して写す
type encoded struct{ v any }

func (e encoded) HasValue() bool { return true }
func (e encoded) Get(ptr any) error {
	b, err := json.Marshal(e.v)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, ptr)
}

func setup(t *testing.T) (*fakeTemporal, *daprtest.Fake, http.Handler) {
	f := &fakeTemporal{running: map[string]workflow.Input{}}
	fd, d := daprtest.New(t)
	gin.SetMode(gin.TestMode)
	r := gin.New()
	(&Server{T: f, Store: &history.Store{D: d}, Registries: []string{"docker.io"}, WaitTimeout: 20 * time.Minute}).Register(r)
	return f, fd, r
}

func do(h http.Handler, method, path, body string, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	return w
}

const body = `{"name":"hello","image":"docker.io/nginxinc/nginx-unprivileged:1.29-alpine"}`

func TestCreateStartsTheWorkflowWithDefaults(t *testing.T) {
	f, _, h := setup(t)
	w := do(h, "POST", "/internal/v1/deployments", body, map[string]string{RequestedByHeader: "alice"})
	if w.Code != 202 {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	in := f.running["app-hello"]
	if in.Op != spec.OpDeploy || in.App.Port != 8080 || in.App.Replicas != 1 || in.RequestedBy != "alice" || in.WaitTimeout != 20*time.Minute {
		t.Errorf("%+v", in)
	}
	o := f.starts[0]
	if o.TaskQueue != workflow.TaskQueue || o.WorkflowIDConflictPolicy != enumspb.WORKFLOW_ID_CONFLICT_POLICY_FAIL || !o.WorkflowExecutionErrorWhenAlreadyStarted {
		t.Errorf("%+v", o)
	}
	if _, ok := o.Memo[workflow.MemoTraceParent]; !ok {
		t.Error("memo に traceparent")
	}
}

func TestConcurrentRequestsForTheSameAppConflict(t *testing.T) {
	_, _, h := setup(t)
	if w := do(h, "POST", "/internal/v1/deployments", body, nil); w.Code != 202 {
		t.Fatal(w.Code)
	}
	w := do(h, "DELETE", "/internal/v1/deployments/hello", "", nil)
	if w.Code != 409 || w.Header().Get("Content-Type") != "application/problem+json" {
		t.Errorf("%d %s", w.Code, w.Header())
	}
}

func TestIdempotencyKeyReplaysAndRejectsReuse(t *testing.T) {
	f, fd, h := setup(t)
	hdr := map[string]string{"Idempotency-Key": "k1"}
	first := do(h, "POST", "/internal/v1/deployments", body, hdr)
	second := do(h, "POST", "/internal/v1/deployments", body, hdr)
	if first.Code != 202 || second.Code != 202 || first.Body.String() != second.Body.String() || second.Header().Get("Idempotent-Replayed") != "true" {
		t.Fatalf("%d %s / %d %s", first.Code, first.Body, second.Code, second.Body)
	}
	if len(f.starts) != 1 {
		t.Errorf("Workflow は 1 回だけ始める: %d", len(f.starts))
	}
	if fd.TTL["idem|k1"] != "86400" {
		t.Errorf("鍵は 24 時間で消える: %q", fd.TTL["idem|k1"])
	}
	other := do(h, "POST", "/internal/v1/deployments", `{"name":"hello","image":"docker.io/a/b:2"}`, hdr)
	if other.Code != 422 {
		t.Errorf("同じ鍵で別の依頼: %d", other.Code)
	}
}

func TestValidationErrorsAreProblemJSON(t *testing.T) {
	_, _, h := setup(t)
	w := do(h, "POST", "/internal/v1/deployments", `{"name":"Bad_Name","image":"nginx","port":80}`, nil)
	if w.Code != 422 || w.Header().Get("Content-Type") != "application/problem+json" {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	var p struct {
		Status int `json:"status"`
		Errors []struct {
			Field string `json:"field"`
		} `json:"errors"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &p)
	if p.Status != 422 || len(p.Errors) != 3 {
		t.Errorf("%s", w.Body)
	}
	if w := do(h, "POST", "/internal/v1/deployments", `{`, nil); w.Code != 400 {
		t.Errorf("壊れた JSON: %d", w.Code)
	}
	if w := do(h, "DELETE", "/internal/v1/deployments/BAD", "", nil); w.Code != 422 {
		t.Errorf("壊れた名前の削除: %d", w.Code)
	}
}

func TestIfMatchIsAnOptimisticLock(t *testing.T) {
	_, fd, h := setup(t)
	if w := do(h, "DELETE", "/internal/v1/deployments/hello", "", map[string]string{"If-Match": "1"}); w.Code != 412 {
		t.Errorf("履歴が無いのに If-Match: %d", w.Code)
	}
	fd.Data[history.AppKey("hello")] = json.RawMessage(`{"name":"hello","deployed":true}`)
	fd.Ver[history.AppKey("hello")] = 3
	if w := do(h, "DELETE", "/internal/v1/deployments/hello", "", map[string]string{"If-Match": "2"}); w.Code != 412 {
		t.Errorf("古い ETag: %d", w.Code)
	}
	if w := do(h, "DELETE", "/internal/v1/deployments/hello", "", map[string]string{"If-Match": "3"}); w.Code != 202 {
		t.Errorf("今の ETag: %d %s", w.Code, w.Body)
	}
}

func TestGetCombinesHistoryAndTheRunningWorkflow(t *testing.T) {
	_, fd, h := setup(t)
	if w := do(h, "GET", "/internal/v1/deployments/hello", "", nil); w.Code != 404 {
		t.Errorf("何も無い: %d", w.Code)
	}
	fd.Data[history.AppKey("hello")] = json.RawMessage(`{"name":"hello","deployed":true,"history":[]}`)
	fd.Ver[history.AppKey("hello")] = 1
	do(h, "POST", "/internal/v1/deployments", body, nil)
	w := do(h, "GET", "/internal/v1/deployments/hello", "", nil)
	var d Deployment
	_ = json.Unmarshal(w.Body.Bytes(), &d)
	if w.Code != 200 || w.Header().Get("ETag") != "1" || !d.Deployed || d.Operation == nil || d.Operation.Phase != workflow.PhaseWaitingArgo || d.Operation.PR.Number != 9 {
		t.Errorf("%d %s", w.Code, w.Body)
	}
}

func TestListPaging(t *testing.T) {
	_, _, h := setup(t)
	if w := do(h, "GET", "/internal/v1/deployments?pageSize=0", "", nil); w.Code != 400 {
		t.Errorf("%d", w.Code)
	}
	w := do(h, "GET", "/internal/v1/deployments", "", nil)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"items":[]`) {
		t.Errorf("%d %s", w.Code, w.Body)
	}
}
