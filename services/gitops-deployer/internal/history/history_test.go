package history

import (
	"strings"
	"testing"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr/daprtest"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

func newStore(t *testing.T) (*daprtest.Fake, *Store) {
	f, d := daprtest.New(t)
	return f, &Store{D: d}
}

func rec(app string, op spec.Op, run, result string) workflow.Record {
	r := workflow.Record{App: app, Op: op, Result: result, WorkflowID: workflow.ID(app), RunID: run}
	if op == spec.OpDeploy {
		r.Spec = &spec.App{Name: app, Image: "docker.io/a/b:1", Port: 8080, Replicas: 1}
	}
	return r
}

func TestAppendTracksTheCurrentStateAndIsIdempotent(t *testing.T) {
	_, s := newStore(t)
	ctx := t.Context()
	for _, r := range []workflow.Record{
		rec("hello", spec.OpDeploy, "r1", workflow.ResultSucceeded),
		rec("hello", spec.OpDeploy, "r1", workflow.ResultSucceeded), // 再試行の重複
		rec("hello", spec.OpDelete, "r2", workflow.ResultFailed),    // 失敗した削除では消えない
	} {
		if err := s.Append(ctx, r); err != nil {
			t.Fatal(err)
		}
	}
	st, etag, found, err := s.Get(ctx, "hello")
	if err != nil || !found || etag == "" {
		t.Fatal(err, found, etag)
	}
	if len(st.History) != 2 || !st.Deployed || st.Current == nil || st.Last == nil || st.Last.RunID != "r2" {
		t.Errorf("%+v", st)
	}
	if err := s.Append(ctx, rec("hello", spec.OpDelete, "r3", workflow.ResultSucceeded)); err != nil {
		t.Fatal(err)
	}
	st, _, _, _ = s.Get(ctx, "hello")
	if st.Deployed || st.Current != nil || st.History[0].RunID != "r3" {
		t.Errorf("削除の後: %+v", st)
	}
}

func TestAppendRereadsOnConflict(t *testing.T) {
	f, s := newStore(t)
	f.Conflicts = 2
	if err := s.Append(t.Context(), rec("a", spec.OpDeploy, "r1", workflow.ResultSucceeded)); err != nil {
		t.Fatal(err)
	}
	st, _, found, _ := s.Get(t.Context(), "a")
	if !found || len(st.History) != 1 {
		t.Errorf("%+v", st)
	}
}

func TestListPages(t *testing.T) {
	_, s := newStore(t)
	for _, n := range []string{"c", "a", "b"} {
		if err := s.Append(t.Context(), rec(n, spec.OpDeploy, "r-"+n, workflow.ResultSucceeded)); err != nil {
			t.Fatal(err)
		}
	}
	page, next, err := s.List(t.Context(), "", 2)
	if err != nil || len(page) != 2 || page[0].Name != "a" || page[1].Name != "b" || next != "2" {
		t.Fatalf("%v %+v %q", err, page, next)
	}
	page, next, err = s.List(t.Context(), next, 2)
	if err != nil || len(page) != 1 || page[0].Name != "c" || next != "" {
		t.Fatalf("%v %+v %q", err, page, next)
	}
	if _, _, err := s.List(t.Context(), "x", 2); err == nil {
		t.Error("読めない pageToken")
	}
}

func TestPublishUsesAStableCloudEventID(t *testing.T) {
	f, s := newStore(t)
	a := &Activities{Store: s}
	r := rec("hello", spec.OpDeploy, "run1", workflow.ResultSucceeded)
	if err := a.PublishCompleted(t.Context(), r); err != nil {
		t.Fatal(err)
	}
	if len(f.Published) != 1 || !strings.Contains(f.Published[0], "metadata.cloudevent.id=app-hello%2Frun1") ||
		!strings.HasPrefix(f.Published[0], "/v1.0/publish/pubsub/gitops-deployer-events?") {
		t.Errorf("%v", f.Published)
	}
}
