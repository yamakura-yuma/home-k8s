// Package workflow は Workflow Engine Pod (workflow-engine) が実行する Workflow と、Pod の間で受け渡す型。
// Workflow は手順・待機・タイムアウト・Signal・補償を一元管理し、外部 I/O は Activity (別の Pod・Task Queue) に任せる。
// Activity は名前で呼ぶ (workflow-engine は GitHub・Dapr の実装を import しない)。
package workflow

import (
	"time"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
)

// Task Queue (合意書の Pod の構成)。Activity は種類ごとに Pod と Task Queue を分ける。
const (
	TaskQueue      = "workflow-engine"
	GitTaskQueue   = "git-activity"
	StoreTaskQueue = "store-activity"
)

const (
	WorkflowName = "Deploy"
	// event-receiver が送る Signal。中身は ArgoEvent
	SignalArgo = "argo-event"
	// api が読む Query。中身は Status
	QueryStatus = "status"
	// Workflow の memo の鍵。開始した bff→api の trace の traceparent。event-receiver が同じ trace に繋ぐ
	MemoTraceParent = "traceparent"
)

// Activity の名前
const (
	ActOpenPR      = "OpenPR"
	ActPRIncluded  = "PRIncluded"
	ActCompensate  = "Compensate"
	ActRecord      = "RecordHistory"
	ActPublishDone = "PublishCompleted"
)

// Activity が返す、打ち直しても変わらない失敗の種類 (RetryPolicy.NonRetryableErrorTypes)
const (
	ErrNotFound = "NotFound"
	ErrInvalid  = "Invalid"
)

// ID は Workflow ID。アプリごとに 1 つ (app-<名前>)。同じアプリの処理は同時に 1 つだけ走る
// (WorkflowIDConflictPolicy FAIL)。終われば同じ ID で次を始められる (ReusePolicy ALLOW_DUPLICATE)。
func ID(name string) string { return "app-" + name }

type Input struct {
	Op          spec.Op  `json:"op"`
	App         spec.App `json:"app"`
	RequestedBy string   `json:"requestedBy"`
	// Argo CD の Synced/Healthy (削除なら Application の消滅) を待つ上限。過ぎたら補償して失敗にする
	WaitTimeout time.Duration `json:"waitTimeout"`
}

// ArgoEvent は ApiServerSource が出した Application の変化 1 つ (event-receiver が CloudEvent から作る)。
type ArgoEvent struct {
	ID       string `json:"id"` // CloudEvent の id。Workflow はこれで重複を捨てる
	Type     string `json:"type"`
	App      string `json:"app"`
	Exists   bool   `json:"exists"` // delete のイベントなら false
	Sync     string `json:"sync"`
	Health   string `json:"health"`
	Revision string `json:"revision"`
}

type PR struct {
	Number    int    `json:"number"`
	URL       string `json:"url"`
	Branch    string `json:"branch"`
	Unchanged bool   `json:"unchanged"` // main と同じ中身で、PR を作らなかった
}

const (
	PhaseOpeningPR    = "opening-pr"
	PhaseWaitingArgo  = "waiting-for-argocd"
	PhaseCompensating = "compensating"
	PhaseRecording    = "recording"
	PhaseDone         = "done"
)

// Status は Query で返す途中の状態。
type Status struct {
	Phase     string     `json:"phase"`
	PR        *PR        `json:"pr,omitempty"`
	LastEvent *ArgoEvent `json:"lastEvent,omitempty"`
	Error     string     `json:"error,omitempty"`
}

const (
	ResultSucceeded = "succeeded"
	ResultUnchanged = "unchanged"
	ResultFailed    = "failed"
)

// Record は履歴 1 件 (Dapr state に残し、完了イベントとして pub/sub に流す)。
type Record struct {
	App         string    `json:"app"`
	Op          spec.Op   `json:"op"`
	Spec        *spec.App `json:"spec,omitempty"`
	Result      string    `json:"result"`
	Error       string    `json:"error,omitempty"`
	PR          *PR       `json:"pr,omitempty"`
	Revision    string    `json:"revision,omitempty"`
	Compensated bool      `json:"compensated,omitempty"`
	WorkflowID  string    `json:"workflowId"`
	RunID       string    `json:"runId"`
	RequestedBy string    `json:"requestedBy"`
	StartedAt   time.Time `json:"startedAt"`
	FinishedAt  time.Time `json:"finishedAt"`
}
