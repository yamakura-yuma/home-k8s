package workflow

import (
	"errors"
	"fmt"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
)

// Deploy はデプロイと削除の Workflow (Input.Op で分ける)。
//
//  1. git-activity: deploys/<名前>/ を足す (消す) PR を作り auto-merge を有効にする
//  2. Signal (argo-event) を待つ。ポーリングしない。デプロイは Application が Synced/Healthy で、その revision に PR の
//     merge が入っていること (git-activity に確かめさせる)、削除は Application の消滅。上限 WaitTimeout
//  3. 待ちきれなければ補償 (Saga): PR が merge 前なら閉じ、merge 後なら元に戻す PR を作る
//  4. store-activity: 履歴を Dapr state に残し、完了イベントを Dapr pub/sub に流す
//
// 決定性: 時刻は workflow.Now、待ちは workflow のタイマーと Signal、乱数・I/O・map の順序に頼る分岐は持たない。
func Deploy(ctx workflow.Context, in Input) (Record, error) {
	info := workflow.GetInfo(ctx)
	rec := Record{
		App: in.App.Name, Op: in.Op, WorkflowID: info.WorkflowExecution.ID, RunID: info.WorkflowExecution.RunID,
		RequestedBy: in.RequestedBy, StartedAt: workflow.Now(ctx),
	}
	if in.Op == spec.OpDeploy {
		app := in.App
		rec.Spec = &app
	}
	st := &Status{Phase: PhaseOpeningPR}
	if err := workflow.SetQueryHandler(ctx, QueryStatus, func() (Status, error) { return *st, nil }); err != nil {
		return rec, err
	}
	logger := workflow.GetLogger(ctx)

	gitCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           GitTaskQueue,
		StartToCloseTimeout: 2 * time.Minute,
		// git-activity の Pod が落ちても、別の Pod (作り直された Pod) が拾い直す。GitHub の一時的な失敗も同じ
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval: 2 * time.Second, BackoffCoefficient: 2, MaximumInterval: time.Minute, MaximumAttempts: 20,
			NonRetryableErrorTypes: []string{ErrNotFound, ErrInvalid},
		},
	})
	storeCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           StoreTaskQueue,
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval: time.Second, BackoffCoefficient: 2, MaximumInterval: 30 * time.Second, MaximumAttempts: 20,
		},
	})

	var wfErr error
	var pr PR
	if err := workflow.ExecuteActivity(gitCtx, ActOpenPR, in).Get(ctx, &pr); err != nil {
		wfErr = fmt.Errorf("PR を作れない: %w", err)
	} else {
		st.PR, rec.PR = &pr, &pr
		if !pr.Unchanged {
			st.Phase = PhaseWaitingArgo
			rev, err := waitForArgo(ctx, gitCtx, in, pr, st)
			if err != nil {
				wfErr = err
				st.Phase = PhaseCompensating
				// 補償は取り消し (cancel) の後でも走らせる
				dctx, _ := workflow.NewDisconnectedContext(gitCtx)
				if cerr := workflow.ExecuteActivity(dctx, ActCompensate, in, pr).Get(dctx, nil); cerr != nil {
					wfErr = errors.Join(wfErr, fmt.Errorf("補償に失敗: %w", cerr))
				} else {
					rec.Compensated = true
				}
			}
			rec.Revision = rev
		}
	}

	st.Phase = PhaseRecording
	switch {
	case wfErr != nil:
		rec.Result, rec.Error, st.Error = ResultFailed, wfErr.Error(), wfErr.Error()
	case pr.Unchanged:
		rec.Result = ResultUnchanged
	default:
		rec.Result = ResultSucceeded
	}
	rec.FinishedAt = workflow.Now(ctx)
	dctx, _ := workflow.NewDisconnectedContext(storeCtx)
	if err := workflow.ExecuteActivity(dctx, ActRecord, rec).Get(dctx, nil); err != nil {
		return rec, errors.Join(wfErr, fmt.Errorf("履歴を残せない: %w", err))
	}
	if err := workflow.ExecuteActivity(dctx, ActPublishDone, rec).Get(dctx, nil); err != nil {
		return rec, errors.Join(wfErr, fmt.Errorf("完了イベントを流せない: %w", err))
	}
	st.Phase = PhaseDone
	logger.Info("終わった", "result", rec.Result, "app", rec.App, "op", string(rec.Op))
	if wfErr != nil {
		return rec, temporal.NewNonRetryableApplicationError(wfErr.Error(), "DeployFailed", nil, rec)
	}
	return rec, nil
}

// waitForArgo は Signal を待つ。同じ CloudEvent id の Signal は 2 回目以降を捨てる (at-least-once の配信の重複)。
// 返すのはデプロイを確かめた revision (削除なら空)。
func waitForArgo(ctx, gitCtx workflow.Context, in Input, pr PR, st *Status) (string, error) {
	ch := workflow.GetSignalChannel(ctx, SignalArgo)
	seen := map[string]bool{}
	deadline := workflow.Now(ctx).Add(in.WaitTimeout)
	for {
		remaining := deadline.Sub(workflow.Now(ctx))
		if remaining <= 0 {
			return "", fmt.Errorf("Argo CD の反映を %s 待ったが来ない", in.WaitTimeout)
		}
		var ev ArgoEvent
		if ok, _ := ch.ReceiveWithTimeout(ctx, remaining, &ev); !ok {
			return "", fmt.Errorf("Argo CD の反映を %s 待ったが来ない", in.WaitTimeout)
		}
		if seen[ev.ID] {
			continue
		}
		seen[ev.ID] = true
		evCopy := ev
		st.LastEvent = &evCopy
		switch in.Op {
		case spec.OpDeploy:
			if !ev.Exists || ev.Sync != "Synced" || ev.Health != "Healthy" || ev.Revision == "" {
				continue
			}
		case spec.OpDelete:
			if ev.Exists {
				continue
			}
		}
		// その revision (削除なら今の main) に、この PR の merge が入っているか。入る前の古い Synced/Healthy で終わらないため
		var included bool
		if err := workflow.ExecuteActivity(gitCtx, ActPRIncluded, pr, ev.Revision).Get(ctx, &included); err != nil {
			return "", fmt.Errorf("PR の merge を確かめられない: %w", err)
		}
		if included {
			return ev.Revision, nil
		}
	}
}

// RegisterOptions は Worker に登録するときの名前 (api はこの名前で開始する)。
func RegisterOptions() workflow.RegisterOptions { return workflow.RegisterOptions{Name: WorkflowName} }
