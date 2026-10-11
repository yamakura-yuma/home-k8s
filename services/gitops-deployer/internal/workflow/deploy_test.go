package workflow

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/mock"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
)

// 名前で呼ぶ Activity の形だけを登録する (中身は OnActivity で差し替える)
func registerStubs(env *testsuite.TestWorkflowEnvironment) {
	env.RegisterWorkflowWithOptions(Deploy, RegisterOptions())
	reg := func(fn any, name string) { env.RegisterActivityWithOptions(fn, activity.RegisterOptions{Name: name}) }
	reg(func(context.Context, Input) (PR, error) { return PR{}, nil }, ActOpenPR)
	reg(func(context.Context, PR, string) (bool, error) { return false, nil }, ActPRIncluded)
	reg(func(context.Context, Input, PR) error { return nil }, ActCompensate)
	reg(func(context.Context, Record) error { return nil }, ActRecord)
	reg(func(context.Context, Record) error { return nil }, ActPublishDone)
}

func newEnv(t *testing.T) *testsuite.TestWorkflowEnvironment {
	var s testsuite.WorkflowTestSuite
	env := s.NewTestWorkflowEnvironment()
	registerStubs(env)
	t.Cleanup(func() { env.AssertExpectations(t) })
	return env
}

func input(op spec.Op) Input {
	return Input{Op: op, App: spec.App{Name: "hello", Image: "docker.io/a/b:1", Port: 8080, Replicas: 1}, RequestedBy: "tester", WaitTimeout: 30 * time.Minute}
}

func signalAt(env *testsuite.TestWorkflowEnvironment, after time.Duration, ev ArgoEvent) {
	env.RegisterDelayedCallback(func() { env.SignalWorkflow(SignalArgo, ev) }, after)
}

var pr = PR{Number: 7, URL: "https://github.com/o/r/pull/7", Branch: "gitops-deployer/deploy-hello-abc"}

func TestDeployWaitsForTheMergedRevisionAndDropsDuplicates(t *testing.T) {
	env := newEnv(t)
	env.OnActivity(ActOpenPR, mock.Anything, mock.Anything).Return(pr, nil).Once()
	// 古い revision の Synced/Healthy (PR はまだ入っていない) → 新しい revision の Synced/Healthy
	env.OnActivity(ActPRIncluded, mock.Anything, pr, "old").Return(false, nil).Once()
	env.OnActivity(ActPRIncluded, mock.Anything, pr, "new").Return(true, nil).Once()
	var recorded Record
	env.OnActivity(ActRecord, mock.Anything, mock.Anything).Run(func(a mock.Arguments) { recorded = a.Get(1).(Record) }).Return(nil).Once()
	env.OnActivity(ActPublishDone, mock.Anything, mock.Anything).Return(nil).Once()

	signalAt(env, time.Minute, ArgoEvent{ID: "e1", App: "hello", Exists: true, Sync: "OutOfSync", Health: "Progressing", Revision: "new"})
	signalAt(env, 2*time.Minute, ArgoEvent{ID: "e2", App: "hello", Exists: true, Sync: "Synced", Health: "Healthy", Revision: "old"})
	// 同じ id の 2 回目 (at-least-once の重複) は捨てる。捨てなければ PRIncluded("old") がもう一度呼ばれて落ちる
	signalAt(env, 3*time.Minute, ArgoEvent{ID: "e2", App: "hello", Exists: true, Sync: "Synced", Health: "Healthy", Revision: "old"})
	signalAt(env, 4*time.Minute, ArgoEvent{ID: "e3", App: "hello", Exists: true, Sync: "Synced", Health: "Healthy", Revision: "new"})

	env.ExecuteWorkflow(Deploy, input(spec.OpDeploy))
	if !env.IsWorkflowCompleted() || env.GetWorkflowError() != nil {
		t.Fatalf("完了しない: %v", env.GetWorkflowError())
	}
	var res Record
	if err := env.GetWorkflowResult(&res); err != nil {
		t.Fatal(err)
	}
	if res.Result != ResultSucceeded || res.Revision != "new" || res.PR.Number != 7 || res.Spec == nil || res.RequestedBy != "tester" {
		t.Errorf("%+v", res)
	}
	if recorded.Result != ResultSucceeded {
		t.Errorf("履歴に残したもの: %+v", recorded)
	}
}

func TestDeployStatusQueryWhileWaiting(t *testing.T) {
	env := newEnv(t)
	env.OnActivity(ActOpenPR, mock.Anything, mock.Anything).Return(pr, nil)
	env.OnActivity(ActPRIncluded, mock.Anything, pr, "r").Return(true, nil)
	env.OnActivity(ActRecord, mock.Anything, mock.Anything).Return(nil)
	env.OnActivity(ActPublishDone, mock.Anything, mock.Anything).Return(nil)
	env.RegisterDelayedCallback(func() {
		v, err := env.QueryWorkflow(QueryStatus)
		if err != nil {
			t.Error(err)
			return
		}
		var st Status
		_ = v.Get(&st)
		if st.Phase != PhaseWaitingArgo || st.PR == nil || st.PR.Number != 7 {
			t.Errorf("待っている間の状態: %+v", st)
		}
	}, 30*time.Second)
	signalAt(env, time.Minute, ArgoEvent{ID: "e", Exists: true, Sync: "Synced", Health: "Healthy", Revision: "r"})
	env.ExecuteWorkflow(Deploy, input(spec.OpDeploy))
	if env.GetWorkflowError() != nil {
		t.Fatal(env.GetWorkflowError())
	}
}

func TestDeployTimesOutAndCompensates(t *testing.T) {
	env := newEnv(t)
	env.OnActivity(ActOpenPR, mock.Anything, mock.Anything).Return(pr, nil)
	env.OnActivity(ActCompensate, mock.Anything, mock.Anything, pr).Return(nil).Once()
	var recorded Record
	env.OnActivity(ActRecord, mock.Anything, mock.Anything).Run(func(a mock.Arguments) { recorded = a.Get(1).(Record) }).Return(nil).Once()
	env.OnActivity(ActPublishDone, mock.Anything, mock.Anything).Return(nil).Once()
	// Synced でも Healthy でない (Degraded) ままタイムアウトする
	signalAt(env, time.Minute, ArgoEvent{ID: "e1", Exists: true, Sync: "Synced", Health: "Degraded", Revision: "r"})

	env.ExecuteWorkflow(Deploy, input(spec.OpDeploy))
	err := env.GetWorkflowError()
	var appErr *temporal.ApplicationError
	if !errors.As(err, &appErr) || appErr.Type() != "DeployFailed" {
		t.Fatalf("DeployFailed で失敗するはず: %v", err)
	}
	if recorded.Result != ResultFailed || !recorded.Compensated || recorded.Error == "" {
		t.Errorf("%+v", recorded)
	}
}

func TestDeleteWaitsForTheApplicationToDisappear(t *testing.T) {
	env := newEnv(t)
	env.OnActivity(ActOpenPR, mock.Anything, mock.Anything).Return(pr, nil)
	env.OnActivity(ActPRIncluded, mock.Anything, pr, "").Return(true, nil).Once()
	env.OnActivity(ActRecord, mock.Anything, mock.Anything).Return(nil)
	env.OnActivity(ActPublishDone, mock.Anything, mock.Anything).Return(nil)
	// まだある (update) イベントでは終わらない
	signalAt(env, time.Minute, ArgoEvent{ID: "e1", Exists: true, Sync: "Synced", Health: "Healthy", Revision: "r"})
	signalAt(env, 2*time.Minute, ArgoEvent{ID: "e2", Exists: false})
	env.ExecuteWorkflow(Deploy, input(spec.OpDelete))
	var res Record
	if err := env.GetWorkflowResult(&res); err != nil {
		t.Fatal(err)
	}
	if res.Result != ResultSucceeded || res.Spec != nil || res.Op != spec.OpDelete {
		t.Errorf("%+v", res)
	}
}

func TestUnchangedSkipsTheWait(t *testing.T) {
	env := newEnv(t)
	env.OnActivity(ActOpenPR, mock.Anything, mock.Anything).Return(PR{Unchanged: true}, nil)
	env.OnActivity(ActRecord, mock.Anything, mock.Anything).Return(nil)
	env.OnActivity(ActPublishDone, mock.Anything, mock.Anything).Return(nil)
	env.ExecuteWorkflow(Deploy, input(spec.OpDeploy))
	var res Record
	if err := env.GetWorkflowResult(&res); err != nil {
		t.Fatal(err)
	}
	if res.Result != ResultUnchanged {
		t.Errorf("%+v", res)
	}
}

func TestOpenPRNotFoundIsRecordedWithoutCompensation(t *testing.T) {
	env := newEnv(t)
	env.OnActivity(ActOpenPR, mock.Anything, mock.Anything).
		Return(PR{}, temporal.NewNonRetryableApplicationError("deploys/hello が無い", ErrNotFound, nil))
	var recorded Record
	env.OnActivity(ActRecord, mock.Anything, mock.Anything).Run(func(a mock.Arguments) { recorded = a.Get(1).(Record) }).Return(nil)
	env.OnActivity(ActPublishDone, mock.Anything, mock.Anything).Return(nil)
	env.ExecuteWorkflow(Deploy, input(spec.OpDelete))
	if env.GetWorkflowError() == nil {
		t.Fatal("失敗するはず")
	}
	if recorded.Result != ResultFailed || recorded.Compensated {
		t.Errorf("%+v", recorded)
	}
}
