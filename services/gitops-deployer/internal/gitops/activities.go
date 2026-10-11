package gitops

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"log/slog"
	"strings"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

// Activities は Task Queue git-activity の Activity。Workflow は名前 (workflow.ActOpenPR など) で呼ぶ。
// どれも打ち直してよい (同じ branch の PR があればそれを返す、閉じた PR は閉じたまま) ので、Pod が途中で消えても
// Temporal が別の試行で続きをやり直せる。
type Activities struct {
	GH         *GitHub
	Registries []string
}

// Register は Worker に名前付きで登録する。
func (a *Activities) Register(r interface {
	RegisterActivityWithOptions(any, activity.RegisterOptions)
}) {
	r.RegisterActivityWithOptions(a.OpenPR, activity.RegisterOptions{Name: workflow.ActOpenPR})
	r.RegisterActivityWithOptions(a.PRIncluded, activity.RegisterOptions{Name: workflow.ActPRIncluded})
	r.RegisterActivityWithOptions(a.Compensate, activity.RegisterOptions{Name: workflow.ActCompensate})
}

func branchName(op spec.Op, name, runID string) string {
	if len(runID) > 8 {
		runID = runID[:8]
	}
	return fmt.Sprintf("gitops-deployer/%s-%s-%s", op, name, runID)
}

func invalid(msg string) error {
	return temporal.NewNonRetryableApplicationError(msg, workflow.ErrInvalid, nil)
}

// OpenPR は deploys/<名前>/ を足す (消す) PR を作り、auto-merge を有効にする。
func (a *Activities) OpenPR(ctx context.Context, in workflow.Input) (workflow.PR, error) {
	// Workflow の外から来た値をもう一度確かめる (マニフェスト注入の防止は api と二重)
	switch in.Op {
	case spec.OpDeploy:
		if err := in.App.Validate(a.Registries); err != nil {
			return workflow.PR{}, invalid(err.Error())
		}
	case spec.OpDelete:
		if err := spec.ValidateName(in.App.Name); err != nil {
			return workflow.PR{}, invalid(err.Error())
		}
	default:
		return workflow.PR{}, invalid("op が deploy でも delete でもない: " + string(in.Op))
	}
	info := activity.GetInfo(ctx)
	branch := branchName(in.Op, in.App.Name, info.WorkflowExecution.RunID)
	log := slog.With("app", in.App.Name, "op", string(in.Op), "branch", branch, "attempt", info.Attempt)

	// 前の試行がすでに PR まで作っていれば、それを使う (auto-merge の有効化だけやり直す)
	if p, err := a.GH.findPull(ctx, branch); err != nil {
		return workflow.PR{}, err
	} else if p != nil {
		log.InfoContext(ctx, "前の試行の PR を使う", "pr", p.Number)
		if p.State == "open" {
			if err := a.GH.enableAutoMerge(ctx, *p); err != nil {
				return workflow.PR{}, err
			}
		}
		return workflow.PR{Number: p.Number, URL: p.HTMLURL, Branch: branch}, nil
	}

	head, err := a.GH.headSHA(ctx)
	if err != nil {
		return workflow.PR{}, err
	}
	var changes []change
	var title string
	switch in.Op {
	case spec.OpDeploy:
		content, err := spec.Render(in.App)
		if err != nil {
			return workflow.PR{}, invalid(err.Error())
		}
		current, found, err := a.GH.fileContent(ctx, spec.File(in.App.Name), head)
		if err != nil {
			return workflow.PR{}, err
		}
		if found && bytes.Equal(current, content) {
			log.InfoContext(ctx, "main と同じ中身なので PR を作らない")
			return workflow.PR{Unchanged: true}, nil
		}
		changes = []change{{Path: spec.File(in.App.Name), Content: content}}
		title = fmt.Sprintf("gitops-deployer: deploy %s (%s)", in.App.Name, in.App.Image)
	case spec.OpDelete:
		base, err := a.GH.commit(ctx, head)
		if err != nil {
			return workflow.PR{}, err
		}
		files, err := a.GH.filesUnder(ctx, base.Tree.SHA, spec.Dir(in.App.Name))
		if err != nil {
			return workflow.PR{}, err
		}
		if len(files) == 0 {
			return workflow.PR{}, temporal.NewNonRetryableApplicationError(spec.Dir(in.App.Name)+" が main に無い", workflow.ErrNotFound, nil)
		}
		for _, f := range files {
			changes = append(changes, change{Path: f.Path, Delete: true})
		}
		title = "gitops-deployer: delete " + in.App.Name
	}
	body := fmt.Sprintf("gitops-deployer の Workflow `%s` (run `%s`) が作った PR。依頼者: %s。\n\n%s の下だけを変える。必須のチェックが通れば auto-merge される。",
		info.WorkflowExecution.ID, info.WorkflowExecution.RunID, in.RequestedBy, spec.Dir(in.App.Name))
	if err := a.GH.commitChanges(ctx, head, branch, title, changes); err != nil {
		return workflow.PR{}, err
	}
	p, err := a.GH.openPull(ctx, branch, title, body)
	if err != nil {
		return workflow.PR{}, err
	}
	if err := a.GH.enableAutoMerge(ctx, p); err != nil {
		return workflow.PR{}, err
	}
	log.InfoContext(ctx, "PR を作り auto-merge を有効にした", "pr", p.Number, "url", p.HTMLURL)
	return workflow.PR{Number: p.Number, URL: p.HTMLURL, Branch: branch}, nil
}

// PRIncluded は PR が merge され、その merge commit が revision に入っているか (revision が空なら merge されたか)。
func (a *Activities) PRIncluded(ctx context.Context, pr workflow.PR, revision string) (bool, error) {
	p, err := a.GH.getPull(ctx, pr.Number)
	if err != nil {
		return false, err
	}
	if !p.Merged {
		return false, nil
	}
	if revision == "" {
		return true, nil
	}
	return a.GH.contains(ctx, p.MergeCommitSHA, revision)
}

// Compensate は Saga の補償。merge 前なら PR を閉じて branch を消す。merge 後なら deploys/<名前>/ を merge の前の
// 状態に戻す PR を作り auto-merge にする (戻す PR もこの Activity の再試行で 1 つにまとまる)。
func (a *Activities) Compensate(ctx context.Context, in workflow.Input, pr workflow.PR) error {
	p, err := a.GH.getPull(ctx, pr.Number)
	if err != nil {
		return err
	}
	log := slog.With("app", in.App.Name, "pr", p.Number)
	switch {
	case p.State == "open":
		log.InfoContext(ctx, "補償: merge 前の PR を閉じる")
		return a.GH.closePull(ctx, p)
	case !p.Merged:
		return nil
	}
	branch := fmt.Sprintf("gitops-deployer/revert-%d", p.Number)
	if existing, err := a.GH.findPull(ctx, branch); err != nil {
		return err
	} else if existing != nil {
		return nil
	}
	merged, err := a.GH.commit(ctx, p.MergeCommitSHA)
	if err != nil {
		return err
	}
	if len(merged.Parents) == 0 {
		return invalid("merge commit に親が無い")
	}
	parent, err := a.GH.commit(ctx, merged.Parents[0].SHA)
	if err != nil {
		return err
	}
	head, err := a.GH.headSHA(ctx)
	if err != nil {
		return err
	}
	headCommit, err := a.GH.commit(ctx, head)
	if err != nil {
		return err
	}
	dir := spec.Dir(in.App.Name)
	before, err := a.GH.filesUnder(ctx, parent.Tree.SHA, dir)
	if err != nil {
		return err
	}
	now, err := a.GH.filesUnder(ctx, headCommit.Tree.SHA, dir)
	if err != nil {
		return err
	}
	changes := revertChanges(before, now)
	if len(changes) == 0 {
		return nil
	}
	title := fmt.Sprintf("gitops-deployer: revert #%d (%s %s)", p.Number, in.Op, in.App.Name)
	if err := a.GH.commitChanges(ctx, head, branch, title, changes); err != nil {
		return err
	}
	rp, err := a.GH.openPull(ctx, branch, title, fmt.Sprintf("#%d の後に Argo CD の反映を確かめられなかったので、%s を merge の前に戻す (Saga の補償)。", p.Number, dir))
	if err != nil {
		return err
	}
	log.InfoContext(ctx, "補償: 戻す PR を作った", "revert_pr", rp.Number)
	return a.GH.enableAutoMerge(ctx, rp)
}

// revertChanges は dir の中身を before に戻す変更 (now にあって before に無いものは消し、before のものは元の blob に戻す)。
func revertChanges(before, now []treeEntry) []change {
	old := map[string]string{}
	for _, e := range before {
		old[e.Path] = e.SHA
	}
	var out []change
	for _, e := range now {
		if sha, ok := old[e.Path]; !ok {
			out = append(out, change{Path: e.Path, Delete: true})
		} else if sha != e.SHA {
			out = append(out, change{Path: e.Path, SHA: sha})
		}
		delete(old, e.Path)
	}
	for _, e := range before {
		if _, ok := old[e.Path]; ok {
			out = append(out, change{Path: e.Path, SHA: e.SHA})
		}
	}
	return out
}

func decodeBase64(s string) ([]byte, error) {
	return base64.StdEncoding.DecodeString(strings.ReplaceAll(s, "\n", ""))
}
