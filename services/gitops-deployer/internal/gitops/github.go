// Package gitops は git-activity の Activity。GitHub の REST と GraphQL で home-k8s に deploys/<名前>/ を変える
// ブランチと PR を作り、auto-merge を有効にする。main には直接 push しない。資格情報は GitHub App の
// installation access token (home-k8s の contents・pull requests の書き込みだけに絞って取る。appauth.go)。
package gitops

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/retry"
)

type GitHub struct {
	API     string // https://api.github.com
	GraphQL string // https://api.github.com/graphql
	Owner   string
	Repo    string
	Base    string // main
	Token   TokenSource
	HTTP    *http.Client
	Retry   retry.Policy
}

// TokenSource は API を打つたびに使うトークン。Invalidate は GitHub が 401 を返したときに呼ぶ (次に取り直す)。
type TokenSource interface {
	Token(ctx context.Context) (string, error)
	Invalidate()
}

func NewGitHub(owner, repo, base string, token TokenSource) *GitHub {
	return &GitHub{
		API: "https://api.github.com", GraphQL: "https://api.github.com/graphql",
		Owner: owner, Repo: repo, Base: base, Token: token,
		HTTP:  &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport)},
		Retry: retry.Policy{Attempts: 4, Base: 500 * time.Millisecond, Max: 8 * time.Second},
	}
}

// APIError は GitHub の 4xx・5xx。
type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string { return fmt.Sprintf("github: %d %s", e.Status, e.Message) }

func statusOf(err error) int {
	var e *APIError
	if errors.As(err, &e) {
		return e.Status
	}
	return 0
}

func (g *GitHub) repoPath(format string, args ...any) string {
	return "/repos/" + g.Owner + "/" + g.Repo + fmt.Sprintf(format, args...)
}

// call は 5xx・429・通信の失敗を指数バックオフ (jitter 付き) で打ち直す。401 はトークンを捨てて 1 度だけ打ち直す
// (取り直したトークンで通らなければそのまま返す)。それ以外の 4xx はすぐ返す。
func (g *GitHub) call(ctx context.Context, method, rawURL string, body, out any) error {
	for attempt := 0; ; attempt++ {
		tok, err := g.Token.Token(ctx)
		if err != nil {
			return err
		}
		err = g.do(ctx, method, rawURL, "Bearer "+tok, body, out)
		if statusOf(err) == http.StatusUnauthorized && attempt == 0 {
			g.Token.Invalidate()
			continue
		}
		return err
	}
}

// do は 1 つの要求を authorization で打つ (打ち直しは call と同じ規則)。
func (g *GitHub) do(ctx context.Context, method, rawURL, authorization string, body, out any) error {
	var payload []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = b
	}
	pol := g.Retry
	if pol.Attempts == 0 {
		pol = retry.Policy{Attempts: 4, Base: 500 * time.Millisecond, Max: 8 * time.Second}
	}
	return retry.Do(ctx, pol, func(ctx context.Context) error {
		var rd io.Reader
		if payload != nil {
			rd = bytes.NewReader(payload)
		}
		req, err := http.NewRequestWithContext(ctx, method, rawURL, rd)
		if err != nil {
			return retry.Permanent(err)
		}
		req.Header.Set("Authorization", authorization)
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		resp, err := g.HTTP.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		if resp.StatusCode >= 400 {
			var m struct {
				Message string `json:"message"`
			}
			_ = json.Unmarshal(b, &m)
			apiErr := &APIError{resp.StatusCode, m.Message}
			if resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests {
				return apiErr
			}
			return retry.Permanent(apiErr)
		}
		return decodeJSON(b, out)
	})
}

func (g *GitHub) rest(ctx context.Context, method, path string, body, out any) error {
	return g.call(ctx, method, g.API+path, body, out)
}

type treeEntry struct {
	Path string `json:"path"`
	Mode string `json:"mode"`
	Type string `json:"type"`
	SHA  string `json:"sha"`
}

func (g *GitHub) headSHA(ctx context.Context) (string, error) {
	var ref struct {
		Object struct {
			SHA string `json:"sha"`
		} `json:"object"`
	}
	err := g.rest(ctx, "GET", g.repoPath("/git/ref/heads/%s", g.Base), nil, &ref)
	return ref.Object.SHA, err
}

type commit struct {
	SHA  string `json:"sha"`
	Tree struct {
		SHA string `json:"sha"`
	} `json:"tree"`
	Parents []struct {
		SHA string `json:"sha"`
	} `json:"parents"`
}

func (g *GitHub) commit(ctx context.Context, sha string) (commit, error) {
	var c commit
	err := g.rest(ctx, "GET", g.repoPath("/git/commits/%s", sha), nil, &c)
	return c, err
}

// filesUnder は commit の tree のうち dir の下のファイル。
func (g *GitHub) filesUnder(ctx context.Context, treeSHA, dir string) ([]treeEntry, error) {
	var t struct {
		Tree      []treeEntry `json:"tree"`
		Truncated bool        `json:"truncated"`
	}
	if err := g.rest(ctx, "GET", g.repoPath("/git/trees/%s?recursive=1", treeSHA), nil, &t); err != nil {
		return nil, err
	}
	if t.Truncated {
		return nil, fmt.Errorf("tree %s が大きすぎて一覧が切れた", treeSHA)
	}
	var out []treeEntry
	for _, e := range t.Tree {
		if e.Type == "blob" && strings.HasPrefix(e.Path, dir+"/") {
			out = append(out, e)
		}
	}
	return out, nil
}

// fileContent は ref の path の中身。無ければ found=false。
func (g *GitHub) fileContent(ctx context.Context, path, ref string) (content []byte, found bool, err error) {
	var f struct {
		Content  string `json:"content"`
		Encoding string `json:"encoding"`
	}
	err = g.rest(ctx, "GET", g.repoPath("/contents/%s?ref=%s", path, url.QueryEscape(ref)), nil, &f)
	if statusOf(err) == http.StatusNotFound {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	b, err := decodeBase64(f.Content)
	return b, true, err
}

type pull struct {
	Number         int    `json:"number"`
	HTMLURL        string `json:"html_url"`
	NodeID         string `json:"node_id"`
	State          string `json:"state"`
	Merged         bool   `json:"merged"`
	MergeCommitSHA string `json:"merge_commit_sha"`
	Head           struct {
		Ref string `json:"ref"`
	} `json:"head"`
}

func (g *GitHub) findPull(ctx context.Context, branch string) (*pull, error) {
	var pulls []pull
	q := url.Values{"head": {g.Owner + ":" + branch}, "state": {"all"}}
	if err := g.rest(ctx, "GET", g.repoPath("/pulls?%s", q.Encode()), nil, &pulls); err != nil {
		return nil, err
	}
	if len(pulls) == 0 {
		return nil, nil
	}
	return &pulls[0], nil
}

func (g *GitHub) getPull(ctx context.Context, number int) (pull, error) {
	var p pull
	err := g.rest(ctx, "GET", g.repoPath("/pulls/%d", number), nil, &p)
	return p, err
}

// change は tree の 1 項目。Delete なら消す (GitHub の API では sha: null)。
type change struct {
	Path    string
	Content []byte // 中身を書く
	SHA     string // 既存の blob を置く
	Delete  bool
}

// commitChanges は base の上に changes を載せた commit を作り、branch をそこへ向ける (あれば上書き)。
func (g *GitHub) commitChanges(ctx context.Context, baseSHA, branch, message string, changes []change) error {
	if err := underRoot(changes); err != nil {
		return err
	}
	base, err := g.commit(ctx, baseSHA)
	if err != nil {
		return err
	}
	entries := make([]map[string]any, 0, len(changes))
	for _, c := range changes {
		e := map[string]any{"path": c.Path, "mode": "100644", "type": "blob"}
		switch {
		case c.Delete:
			e["sha"] = nil
		case c.SHA != "":
			e["sha"] = c.SHA
		default:
			e["content"] = string(c.Content)
		}
		entries = append(entries, e)
	}
	var tree struct {
		SHA string `json:"sha"`
	}
	if err := g.rest(ctx, "POST", g.repoPath("/git/trees"), map[string]any{"base_tree": base.Tree.SHA, "tree": entries}, &tree); err != nil {
		return err
	}
	var c struct {
		SHA string `json:"sha"`
	}
	if err := g.rest(ctx, "POST", g.repoPath("/git/commits"), map[string]any{"message": message, "tree": tree.SHA, "parents": []string{baseSHA}}, &c); err != nil {
		return err
	}
	err = g.rest(ctx, "POST", g.repoPath("/git/refs"), map[string]any{"ref": "refs/heads/" + branch, "sha": c.SHA}, nil)
	if statusOf(err) == http.StatusUnprocessableEntity {
		// 前の試行が branch まで作って落ちた。同じ branch を今の commit に向け直す
		err = g.rest(ctx, "PATCH", g.repoPath("/git/refs/heads/%s", branch), map[string]any{"sha": c.SHA, "force": true}, nil)
	}
	return err
}

// underRoot は PR が deploys/ の下しか触らないことを、GitHub に送る直前にもう一度確かめる (合意書の制約)。
func underRoot(changes []change) error {
	for _, c := range changes {
		if !strings.HasPrefix(c.Path, "deploys/") || strings.Contains(c.Path, "..") {
			return fmt.Errorf("deploys/ の外は変えない: %q", c.Path)
		}
	}
	return nil
}

func (g *GitHub) openPull(ctx context.Context, branch, title, body string) (pull, error) {
	var p pull
	err := g.rest(ctx, "POST", g.repoPath("/pulls"), map[string]any{"title": title, "head": branch, "base": g.Base, "body": body}, &p)
	return p, err
}

// enableAutoMerge は PR の auto-merge (squash) を有効にする。必須のチェック (ci / just ci) が通れば GitHub が merge する。
// チェックがもう終わっていて auto-merge を受け付けない (clean status) ときは、その場で merge する。
func (g *GitHub) enableAutoMerge(ctx context.Context, p pull) error {
	const q = `mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH}) { pullRequest { number } } }`
	var resp struct {
		Errors []struct {
			Message string `json:"message"`
		} `json:"errors"`
	}
	if err := g.call(ctx, "POST", g.GraphQL, map[string]any{"query": q, "variables": map[string]any{"id": p.NodeID}}, &resp); err != nil {
		return err
	}
	if len(resp.Errors) == 0 {
		return nil
	}
	msg := resp.Errors[0].Message
	switch {
	case strings.Contains(msg, "clean status"):
		return g.rest(ctx, "PUT", g.repoPath("/pulls/%d/merge", p.Number), map[string]any{"merge_method": "squash"}, nil)
	case strings.Contains(msg, "already merged") || strings.Contains(msg, "Auto merge is already enabled"):
		return nil
	}
	return fmt.Errorf("auto-merge を有効にできない: %s", msg)
}

func (g *GitHub) closePull(ctx context.Context, p pull) error {
	if err := g.rest(ctx, "PATCH", g.repoPath("/pulls/%d", p.Number), map[string]any{"state": "closed"}, nil); err != nil {
		return err
	}
	err := g.rest(ctx, "DELETE", g.repoPath("/git/refs/heads/%s", p.Head.Ref), nil, nil)
	if s := statusOf(err); s == http.StatusNotFound || s == http.StatusUnprocessableEntity {
		return nil
	}
	return err
}

// contains は revision に commit sha が入っているか (sha から revision へ進んだか同じ)。
func (g *GitHub) contains(ctx context.Context, sha, revision string) (bool, error) {
	var cmp struct {
		Status string `json:"status"`
	}
	err := g.rest(ctx, "GET", g.repoPath("/compare/%s...%s", sha, revision), nil, &cmp)
	if statusOf(err) == http.StatusNotFound {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return cmp.Status == "ahead" || cmp.Status == "identical", nil
}
