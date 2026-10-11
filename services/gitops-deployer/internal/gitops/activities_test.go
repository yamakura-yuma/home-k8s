package gitops

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/retry"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

// fakeGitHub は Activity が使う GitHub の API だけを持つ偽物。main の tree は path → 中身。
type fakeGitHub struct {
	mu        sync.Mutex
	main      map[string]string
	trees     []map[string]any // POST /git/trees の中身
	refs      map[string]string
	pulls     map[int]*pull
	automerge []string
	closed    []int
	compare   string
	fail500   int // 次の n 回を 500 にする (打ち直しの確認)
	calls     []string
}

func newFake() *fakeGitHub {
	return &fakeGitHub{main: map[string]string{"README.md": "x"}, refs: map[string]string{}, pulls: map[int]*pull{}, compare: "ahead"}
}

func (f *fakeGitHub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := strings.TrimPrefix(r.URL.Path, "/repos/o/r")
	f.calls = append(f.calls, r.Method+" "+path)
	if r.Header.Get("Authorization") != "Bearer tok" {
		w.WriteHeader(401)
		return
	}
	if f.fail500 > 0 {
		f.fail500--
		w.WriteHeader(500)
		return
	}
	var body map[string]any
	if b, _ := io.ReadAll(r.Body); len(b) > 0 {
		_ = json.Unmarshal(b, &body)
	}
	reply := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	switch {
	case r.Method == "GET" && path == "/git/ref/heads/main":
		reply(map[string]any{"object": map[string]any{"sha": "head"}})
	case r.Method == "GET" && strings.HasPrefix(path, "/git/commits/"):
		sha := strings.TrimPrefix(path, "/git/commits/")
		reply(map[string]any{"sha": sha, "tree": map[string]any{"sha": "tree-" + sha}, "parents": []any{map[string]any{"sha": "parent"}}})
	case r.Method == "GET" && strings.HasPrefix(path, "/git/trees/"):
		var tree []map[string]any
		files := f.main
		if strings.HasSuffix(path, "tree-parent") {
			files = map[string]string{"deploys/hello/app.yaml": "old"}
		}
		for p, c := range files {
			tree = append(tree, map[string]any{"path": p, "mode": "100644", "type": "blob", "sha": "blob-" + c})
		}
		reply(map[string]any{"tree": tree})
	case r.Method == "GET" && strings.HasPrefix(path, "/contents/"):
		p := strings.TrimPrefix(path, "/contents/")
		c, ok := f.main[p]
		if !ok {
			w.WriteHeader(404)
			reply(map[string]any{"message": "Not Found"})
			return
		}
		reply(map[string]any{"content": base64.StdEncoding.EncodeToString([]byte(c)), "encoding": "base64"})
	case r.Method == "POST" && path == "/git/trees":
		f.trees = append(f.trees, body)
		reply(map[string]any{"sha": "newtree"})
	case r.Method == "POST" && path == "/git/commits":
		reply(map[string]any{"sha": "newcommit"})
	case r.Method == "POST" && path == "/git/refs":
		ref := body["ref"].(string)
		if _, ok := f.refs[ref]; ok {
			w.WriteHeader(422)
			reply(map[string]any{"message": "Reference already exists"})
			return
		}
		f.refs[ref] = body["sha"].(string)
		reply(map[string]any{})
	case r.Method == "GET" && path == "/pulls":
		head := strings.TrimPrefix(r.URL.Query().Get("head"), "o:")
		out := []any{}
		for _, p := range f.pulls {
			if p.Head.Ref == head {
				out = append(out, p)
			}
		}
		reply(out)
	case r.Method == "POST" && path == "/pulls":
		n := len(f.pulls) + 1
		p := &pull{Number: n, HTMLURL: fmt.Sprintf("https://github.com/o/r/pull/%d", n), NodeID: fmt.Sprintf("PR_%d", n), State: "open"}
		p.Head.Ref = body["head"].(string)
		f.pulls[n] = p
		reply(p)
	case r.Method == "GET" && strings.HasPrefix(path, "/pulls/"):
		var n int
		fmt.Sscanf(strings.TrimPrefix(path, "/pulls/"), "%d", &n)
		reply(f.pulls[n])
	case r.Method == "PATCH" && strings.HasPrefix(path, "/pulls/"):
		var n int
		fmt.Sscanf(strings.TrimPrefix(path, "/pulls/"), "%d", &n)
		f.pulls[n].State = "closed"
		f.closed = append(f.closed, n)
		reply(f.pulls[n])
	case r.Method == "DELETE" && strings.HasPrefix(path, "/git/refs/heads/"):
		w.WriteHeader(204)
	case r.Method == "GET" && strings.HasPrefix(path, "/compare/"):
		reply(map[string]any{"status": f.compare})
	case r.Method == "POST" && r.URL.Path == "/graphql":
		f.automerge = append(f.automerge, body["variables"].(map[string]any)["id"].(string))
		reply(map[string]any{"data": map[string]any{}})
	default:
		w.WriteHeader(404)
		reply(map[string]any{"message": "no route " + r.Method + " " + path})
	}
}

func setup(t *testing.T) (*fakeGitHub, *Activities, *testsuite.TestActivityEnvironment) {
	f := newFake()
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	gh := &GitHub{API: srv.URL, GraphQL: srv.URL + "/graphql", Owner: "o", Repo: "r", Base: "main", Token: "tok", HTTP: srv.Client(),
		Retry: retry.Policy{Attempts: 3, Base: time.Millisecond, Max: time.Millisecond}}
	a := &Activities{GH: gh, Registries: []string{"docker.io"}}
	var s testsuite.WorkflowTestSuite
	env := s.NewTestActivityEnvironment()
	env.RegisterActivity(a.OpenPR)
	env.RegisterActivity(a.PRIncluded)
	env.RegisterActivity(a.Compensate)
	return f, a, env
}

var app = spec.App{Name: "hello", Image: "docker.io/nginxinc/nginx-unprivileged:1.29-alpine", Port: 8080, Replicas: 1}

func TestOpenPRDeployWritesOnlyUnderDeploysAndEnablesAutoMerge(t *testing.T) {
	f, a, env := setup(t)
	f.fail500 = 1 // 最初の呼び出しは 500 (打ち直して通る)
	v, err := env.ExecuteActivity(a.OpenPR, workflow.Input{Op: spec.OpDeploy, App: app, RequestedBy: "tester"})
	if err != nil {
		t.Fatal(err)
	}
	var pr workflow.PR
	_ = v.Get(&pr)
	if pr.Number != 1 || !strings.HasPrefix(pr.Branch, "gitops-deployer/deploy-hello-") {
		t.Errorf("%+v", pr)
	}
	if len(f.trees) != 1 {
		t.Fatalf("trees: %v", f.trees)
	}
	entries := f.trees[0]["tree"].([]any)
	e := entries[0].(map[string]any)
	if len(entries) != 1 || e["path"] != "deploys/hello/app.yaml" || !strings.Contains(e["content"].(string), "kind: Deployment") {
		t.Errorf("%v", entries)
	}
	if strings.Join(f.automerge, ",") != "PR_1" {
		t.Errorf("auto-merge: %v", f.automerge)
	}
}

func TestOpenPRIsIdempotentAcrossRetries(t *testing.T) {
	f, a, env := setup(t)
	in := workflow.Input{Op: spec.OpDeploy, App: app}
	for range 2 {
		if _, err := env.ExecuteActivity(a.OpenPR, in); err != nil {
			t.Fatal(err)
		}
	}
	if len(f.pulls) != 1 || len(f.trees) != 1 {
		t.Errorf("2 回目は前の PR を使う: pulls=%d trees=%d", len(f.pulls), len(f.trees))
	}
}

func TestOpenPRUnchangedWhenMainHasTheSameContent(t *testing.T) {
	f, a, env := setup(t)
	content, _ := spec.Render(app)
	f.main["deploys/hello/app.yaml"] = string(content)
	v, err := env.ExecuteActivity(a.OpenPR, workflow.Input{Op: spec.OpDeploy, App: app})
	if err != nil {
		t.Fatal(err)
	}
	var pr workflow.PR
	_ = v.Get(&pr)
	if !pr.Unchanged || len(f.pulls) != 0 {
		t.Errorf("%+v pulls=%d", pr, len(f.pulls))
	}
}

func TestOpenPRDeleteRemovesTheDirectory(t *testing.T) {
	f, a, env := setup(t)
	f.main["deploys/hello/app.yaml"] = "x"
	f.main["deploys/hello2/app.yaml"] = "y"
	if _, err := env.ExecuteActivity(a.OpenPR, workflow.Input{Op: spec.OpDelete, App: spec.App{Name: "hello"}}); err != nil {
		t.Fatal(err)
	}
	entries := f.trees[0]["tree"].([]any)
	e := entries[0].(map[string]any)
	if len(entries) != 1 || e["path"] != "deploys/hello/app.yaml" || e["sha"] != nil {
		t.Errorf("deploys/hello だけを消す (hello2 は残す): %v", entries)
	}
}

func TestOpenPRDeleteOfAMissingAppIsNotFound(t *testing.T) {
	_, a, env := setup(t)
	_, err := env.ExecuteActivity(a.OpenPR, workflow.Input{Op: spec.OpDelete, App: spec.App{Name: "nope"}})
	var appErr *temporal.ApplicationError
	if !asAppErr(err, &appErr) || appErr.Type() != workflow.ErrNotFound {
		t.Fatalf("NotFound のはず: %v", err)
	}
}

func TestOpenPRRejectsInjectedValues(t *testing.T) {
	f, a, env := setup(t)
	bad := app
	bad.Image = "docker.io/x:1\n    command: [sh]"
	_, err := env.ExecuteActivity(a.OpenPR, workflow.Input{Op: spec.OpDeploy, App: bad})
	var appErr *temporal.ApplicationError
	if !asAppErr(err, &appErr) || appErr.Type() != workflow.ErrInvalid {
		t.Fatalf("Invalid のはず: %v", err)
	}
	if len(f.calls) != 0 {
		t.Errorf("GitHub を呼ばない: %v", f.calls)
	}
}

func TestUnderRoot(t *testing.T) {
	for _, p := range []string{"README.md", "clusters/kind/argocd/apps/x.yaml", "deploys/../x", "deploysx/a"} {
		if underRoot([]change{{Path: p}}) == nil {
			t.Errorf("%s を通した", p)
		}
	}
	if err := underRoot([]change{{Path: "deploys/a/app.yaml"}}); err != nil {
		t.Error(err)
	}
}

func TestPRIncluded(t *testing.T) {
	f, a, env := setup(t)
	f.pulls[3] = &pull{Number: 3, Merged: false}
	check := func(rev string) bool {
		v, err := env.ExecuteActivity(a.PRIncluded, workflow.PR{Number: 3}, rev)
		if err != nil {
			t.Fatal(err)
		}
		var ok bool
		_ = v.Get(&ok)
		return ok
	}
	if check("r") {
		t.Error("merge 前は false")
	}
	f.pulls[3].Merged, f.pulls[3].MergeCommitSHA = true, "m"
	if !check("r") || !check("") {
		t.Error("merge 後で revision が先に進んでいれば true")
	}
	f.compare = "behind"
	if check("r") {
		t.Error("revision が merge より前なら false")
	}
}

func TestCompensateClosesAnOpenPR(t *testing.T) {
	f, a, env := setup(t)
	f.pulls[5] = &pull{Number: 5, State: "open"}
	f.pulls[5].Head.Ref = "gitops-deployer/deploy-hello-x"
	if _, err := env.ExecuteActivity(a.Compensate, workflow.Input{Op: spec.OpDeploy, App: app}, workflow.PR{Number: 5}); err != nil {
		t.Fatal(err)
	}
	if len(f.closed) != 1 || f.closed[0] != 5 || len(f.trees) != 0 {
		t.Errorf("closed=%v trees=%d", f.closed, len(f.trees))
	}
}

func TestCompensateRevertsAMergedPR(t *testing.T) {
	f, a, env := setup(t)
	f.main["deploys/hello/app.yaml"] = "new"
	f.main["deploys/hello/extra.yaml"] = "added"
	f.pulls[5] = &pull{Number: 5, State: "closed", Merged: true, MergeCommitSHA: "m"}
	if _, err := env.ExecuteActivity(a.Compensate, workflow.Input{Op: spec.OpDeploy, App: app}, workflow.PR{Number: 5}); err != nil {
		t.Fatal(err)
	}
	if len(f.trees) != 1 {
		t.Fatalf("戻す PR を 1 つ作る: %v", f.trees)
	}
	got := map[string]any{}
	for _, e := range f.trees[0]["tree"].([]any) {
		m := e.(map[string]any)
		got[m["path"].(string)] = m["sha"]
	}
	if got["deploys/hello/app.yaml"] != "blob-old" || got["deploys/hello/extra.yaml"] != nil || len(got) != 2 {
		t.Errorf("merge の前 (parent) に戻す: %v", got)
	}
	if len(f.automerge) != 1 {
		t.Errorf("戻す PR も auto-merge: %v", f.automerge)
	}
}

func asAppErr(err error, target **temporal.ApplicationError) bool {
	if err == nil {
		return false
	}
	return errors.As(err, target)
}
