// Package api は api の本体 (gin)。外には出さず、bff から Dapr の service invocation でだけ呼ばれる
// (AuthorizationPolicy が bff の ServiceAccount 以外を拒む)。Workflow の開始・照会と、履歴 (Dapr state) の読み出し。
//
//   - 冪等性: Idempotency-Key ごとに依頼の要約 (sha256) と応答を Dapr state に 24 時間残す。同じ鍵・同じ依頼は同じ応答を返し
//     (Idempotent-Replayed: true)、同じ鍵・違う依頼は 422
//   - 同時実行制御: Workflow ID はアプリごとに 1 つ (app-<名前>)。走っている間に同じアプリを依頼すると 409。
//     If-Match があれば、履歴の ETag (楽観ロック) が合わないと 412
//   - 入力検証: spec.Validate。誤りは 422 の problem+json の errors に欄ごとに入る
package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/history"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/obs"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/problem"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

// RequestedByHeader は bff が JWT から読んだ呼び出し元 (履歴と監査に残す)。api は外に出ないので bff の値を信じる。
const RequestedByHeader = "X-Requested-By"

type Temporal interface {
	ExecuteWorkflow(ctx context.Context, options client.StartWorkflowOptions, workflow any, args ...any) (client.WorkflowRun, error)
	DescribeWorkflowExecution(ctx context.Context, workflowID, runID string) (*workflowservice.DescribeWorkflowExecutionResponse, error)
	QueryWorkflow(ctx context.Context, workflowID, runID, queryType string, args ...any) (converter.EncodedValue, error)
}

type Server struct {
	T           Temporal
	Store       *history.Store
	Registries  []string
	WaitTimeout time.Duration
}

func (s *Server) Register(r gin.IRoutes) {
	r.POST("/internal/v1/deployments", s.create)
	r.DELETE("/internal/v1/deployments/:name", s.remove)
	r.GET("/internal/v1/deployments", s.list)
	r.GET("/internal/v1/deployments/:name", s.get)
}

// Operation は開始した (走っている・終わった) Workflow 1 つ。
type Operation struct {
	WorkflowID string       `json:"workflowId"`
	RunID      string       `json:"runId"`
	App        string       `json:"app"`
	Op         spec.Op      `json:"op,omitempty"`
	Status     string       `json:"status"`
	Phase      string       `json:"phase,omitempty"`
	PR         *workflow.PR `json:"pr,omitempty"`
	Error      string       `json:"error,omitempty"`
}

type Deployment struct {
	history.AppState
	Operation *Operation `json:"operation,omitempty"`
}

type idemRecord struct {
	Hash     string    `json:"hash"`
	Response Operation `json:"response"`
}

const idemTTLSeconds = 24 * 60 * 60

func (s *Server) create(c *gin.Context) {
	var app spec.App
	if err := c.ShouldBindJSON(&app); err != nil {
		problem.Write(c, http.StatusBadRequest, "Bad Request", "JSON が読めない: "+err.Error())
		return
	}
	app = app.Normalize()
	if err := app.Validate(s.Registries); err != nil {
		var fe spec.Errors
		errors.As(err, &fe)
		problem.WriteErrors(c, http.StatusUnprocessableEntity, "Unprocessable Content", "依頼の値が正しくない", fe)
		return
	}
	s.start(c, spec.OpDeploy, app)
}

func (s *Server) remove(c *gin.Context) {
	name := c.Param("name")
	if err := spec.ValidateName(name); err != nil {
		problem.WriteErrors(c, http.StatusUnprocessableEntity, "Unprocessable Content", "名前が正しくない", err)
		return
	}
	s.start(c, spec.OpDelete, spec.App{Name: name})
}

func requestHash(op spec.Op, app spec.App) string {
	b, _ := json.Marshal(struct {
		Op  spec.Op  `json:"op"`
		App spec.App `json:"app"`
	}{op, app})
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func (s *Server) start(c *gin.Context, op spec.Op, app spec.App) {
	ctx := c.Request.Context()
	key := c.GetHeader("Idempotency-Key")
	if len(key) > 128 {
		problem.Write(c, http.StatusBadRequest, "Bad Request", "Idempotency-Key は 128 文字まで")
		return
	}
	hash := requestHash(op, app)
	idemKey := "idem|" + key
	if key != "" {
		var prev idemRecord
		_, found, err := s.Store.D.GetState(ctx, idemKey, &prev)
		if err != nil {
			problem.Write(c, http.StatusServiceUnavailable, "Service Unavailable", "履歴を読めない: "+err.Error())
			return
		}
		if found {
			if prev.Hash != hash {
				problem.Write(c, http.StatusUnprocessableEntity, "Unprocessable Content", "この Idempotency-Key は別の依頼に使われた")
				return
			}
			c.Header("Idempotent-Replayed", "true")
			c.JSON(http.StatusAccepted, prev.Response)
			return
		}
	}
	if ifMatch := c.GetHeader("If-Match"); ifMatch != "" {
		_, etag, found, err := s.Store.Get(ctx, app.Name)
		if err != nil {
			problem.Write(c, http.StatusServiceUnavailable, "Service Unavailable", "履歴を読めない: "+err.Error())
			return
		}
		if !found || etag != ifMatch {
			problem.Write(c, http.StatusPreconditionFailed, "Precondition Failed", "If-Match の ETag が今の履歴と合わない (ほかの依頼が先に変えた)")
			return
		}
	}
	in := workflow.Input{Op: op, App: app, RequestedBy: c.GetHeader(RequestedByHeader), WaitTimeout: s.WaitTimeout}
	wfID := workflow.ID(app.Name)
	run, err := s.T.ExecuteWorkflow(ctx, client.StartWorkflowOptions{
		ID:                                       wfID,
		TaskQueue:                                workflow.TaskQueue,
		WorkflowIDReusePolicy:                    enumspb.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
		WorkflowIDConflictPolicy:                 enumspb.WORKFLOW_ID_CONFLICT_POLICY_FAIL,
		WorkflowExecutionErrorWhenAlreadyStarted: true,
		WorkflowExecutionTimeout:                 s.WaitTimeout + 30*time.Minute,
		Memo:                                     map[string]any{workflow.MemoTraceParent: obs.TraceParent(ctx)},
	}, workflow.WorkflowName, in)
	var started *serviceerror.WorkflowExecutionAlreadyStarted
	if errors.As(err, &started) {
		problem.Write(c, http.StatusConflict, "Conflict", "アプリ "+app.Name+" の処理がすでに走っている (終わってから依頼し直す)")
		return
	}
	if err != nil {
		problem.Write(c, http.StatusServiceUnavailable, "Service Unavailable", "Workflow を始められない: "+err.Error())
		return
	}
	resp := Operation{WorkflowID: wfID, RunID: run.GetRunID(), App: app.Name, Op: op, Status: "running", Phase: workflow.PhaseOpeningPR}
	if key != "" {
		err := s.Store.D.SaveStateTTL(ctx, idemKey, idemRecord{Hash: hash, Response: resp}, "", idemTTLSeconds)
		if err != nil && !errors.Is(err, dapr.ErrConflict) {
			slog.WarnContext(ctx, "Idempotency-Key を残せない", "error", err.Error())
		}
	}
	slog.InfoContext(ctx, "Workflow を始めた", "workflow_id", wfID, "run_id", resp.RunID, "op", string(op), "app", app.Name)
	c.JSON(http.StatusAccepted, resp)
}

func (s *Server) list(c *gin.Context) {
	size, err := strconv.Atoi(c.DefaultQuery("pageSize", "20"))
	if err != nil || size < 1 || size > 100 {
		problem.Write(c, http.StatusBadRequest, "Bad Request", "pageSize は 1〜100")
		return
	}
	items, next, err := s.Store.List(c.Request.Context(), c.Query("pageToken"), size)
	if err != nil {
		problem.Write(c, http.StatusBadRequest, "Bad Request", err.Error())
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": items, "nextPageToken": next})
}

func (s *Server) get(c *gin.Context) {
	name := c.Param("name")
	if spec.ValidateName(name) != nil {
		problem.Write(c, http.StatusNotFound, "Not Found", "")
		return
	}
	ctx := c.Request.Context()
	st, etag, found, err := s.Store.Get(ctx, name)
	if err != nil {
		problem.Write(c, http.StatusServiceUnavailable, "Service Unavailable", "履歴を読めない: "+err.Error())
		return
	}
	op, err := s.operation(ctx, name)
	if err != nil {
		problem.Write(c, http.StatusServiceUnavailable, "Service Unavailable", "Temporal を読めない: "+err.Error())
		return
	}
	if !found && op == nil {
		problem.Write(c, http.StatusNotFound, "Not Found", "アプリ "+name+" の履歴も Workflow も無い")
		return
	}
	st.Name = name
	if etag != "" {
		c.Header("ETag", etag)
	}
	c.JSON(http.StatusOK, Deployment{AppState: st, Operation: op})
}

// operation はアプリの最新の Workflow の状態。走っていれば Query で途中の段を、終わっていれば結果を読む。
func (s *Server) operation(ctx context.Context, name string) (*Operation, error) {
	wfID := workflow.ID(name)
	desc, err := s.T.DescribeWorkflowExecution(ctx, wfID, "")
	var nf *serviceerror.NotFound
	if errors.As(err, &nf) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	info := desc.GetWorkflowExecutionInfo()
	op := &Operation{WorkflowID: wfID, RunID: info.GetExecution().GetRunId(), App: name, Status: statusName(info.GetStatus())}
	if info.GetStatus() == enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING {
		v, err := s.T.QueryWorkflow(ctx, wfID, op.RunID, workflow.QueryStatus)
		if err == nil {
			var st workflow.Status
			if v.Get(&st) == nil {
				op.Phase, op.PR, op.Error = st.Phase, st.PR, st.Error
			}
		}
	}
	return op, nil
}

func statusName(s enumspb.WorkflowExecutionStatus) string {
	switch s {
	case enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING:
		return "running"
	case enumspb.WORKFLOW_EXECUTION_STATUS_COMPLETED:
		return "completed"
	case enumspb.WORKFLOW_EXECUTION_STATUS_FAILED:
		return "failed"
	case enumspb.WORKFLOW_EXECUTION_STATUS_TIMED_OUT:
		return "timed-out"
	case enumspb.WORKFLOW_EXECUTION_STATUS_TERMINATED:
		return "terminated"
	case enumspb.WORKFLOW_EXECUTION_STATUS_CANCELED:
		return "canceled"
	}
	return "unknown"
}
