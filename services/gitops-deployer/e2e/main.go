// e2e は testkube の TestWorkflow gitops-deployer-e2e の本体 (同じイメージの /usr/local/bin/e2e)。クラスタの中から
// Istio の Gateway (gateway-istio) → APISIX → bff → api を通して、デプロイと削除を 1 回ずつ依頼し、完了条件を確かめる。
//
//  1. トークン無し・不正なトークンは 401
//  2. Keycloak の client credentials のトークンでデプロイを依頼 → 202。同じ Idempotency-Key は同じ応答、走っている間の別の依頼は 409
//  3. Workflow が completed になり、ApplicationSet が作った Application deploy-<名前> が Synced/Healthy、履歴 (Dapr state) が一覧に出る
//  4. Tempo に、依頼の trace (この試験が付けた traceparent) で 6 つの Pod のスパンが揃う
//  5. 削除を依頼 → completed、Application が消え、一覧で deployed=false
//
// 完了イベントの Kafka のトピックは次の段 (Kafka の CLI) が確かめるので、run の ID を OUT に書く。
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"
)

var (
	base     = env("BASE_URL", "http://gateway-istio.istio-ingress.svc.cluster.local/gitops-deployer")
	tokenURL = env("TOKEN_URL", "https://keycloak.taild2b611.ts.net/realms/home-k8s/protocol/openid-connect/token")
	tempo    = env("TEMPO_URL", "http://tempo.observability.svc.cluster.local:3200")
	image    = env("APP_IMAGE", "docker.io/nginxinc/nginx-unprivileged:1.29-alpine")
	timeout  = mustDuration(env("TIMEOUT", "25m"))
	out      = env("OUT", "/data/e2e.env")
	services = []string{"bff", "api", "workflow-engine", "git-activity", "store-activity", "event-receiver"}
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func mustDuration(s string) time.Duration {
	d, err := time.ParseDuration(s)
	if err != nil {
		log.Fatal(err)
	}
	return d
}

func check(ok bool, format string, args ...any) {
	if !ok {
		log.Fatalf("NG: "+format, args...)
	}
	log.Printf("OK: "+format, args...)
}

type resp struct {
	code   int
	header http.Header
	body   []byte
}

func (r resp) json(v any) {
	if err := json.Unmarshal(r.body, v); err != nil {
		log.Fatalf("JSON が読めない (%d): %s", r.code, r.body)
	}
}

func call(method, path, token string, body any, headers map[string]string) resp {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, base+path, rd)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	for attempt := 0; ; attempt++ {
		r, err := http.DefaultClient.Do(req)
		if err != nil {
			if attempt < 5 {
				time.Sleep(3 * time.Second)
				if body != nil {
					b, _ := json.Marshal(body)
					req.Body = io.NopCloser(bytes.NewReader(b))
				}
				continue
			}
			log.Fatalf("%s %s: %v", method, path, err)
		}
		b, _ := io.ReadAll(r.Body)
		r.Body.Close()
		return resp{r.StatusCode, r.Header, b}
	}
}

func clientToken() string {
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {os.Getenv("CLIENT_ID")}, "client_secret": {os.Getenv("CLIENT_SECRET")}}
	r, err := http.PostForm(tokenURL, form)
	if err != nil {
		log.Fatalf("トークンを取れない: %v", err)
	}
	defer r.Body.Close()
	var t struct {
		AccessToken string `json:"access_token"`
	}
	b, _ := io.ReadAll(r.Body)
	if r.StatusCode != 200 || json.Unmarshal(b, &t) != nil || t.AccessToken == "" {
		log.Fatalf("トークンを取れない: %d %s", r.StatusCode, b)
	}
	return t.AccessToken
}

type operation struct {
	WorkflowID string `json:"workflowId"`
	RunID      string `json:"runId"`
	Status     string `json:"status"`
	Phase      string `json:"phase"`
}

type deployment struct {
	Name     string `json:"name"`
	Deployed bool   `json:"deployed"`
	Last     struct {
		RunID  string `json:"runId"`
		Op     string `json:"op"`
		Result string `json:"result"`
		PR     *struct {
			URL string `json:"url"`
		} `json:"pr"`
	} `json:"last"`
	Operation *operation        `json:"operation"`
	Links     map[string]string `json:"links"`
}

// waitDone は Workflow が終わるまで bff の状態を読む (試験の側の待ち。Workflow 自体は Signal を待つ)。
func waitDone(token, name, runID string) deployment {
	deadline := time.Now().Add(timeout)
	last := ""
	for time.Now().Before(deadline) {
		r := call("GET", "/api/v1/deployments/"+name, token, nil, nil)
		if r.code == 200 {
			var d deployment
			r.json(&d)
			if d.Operation != nil && d.Operation.RunID == runID {
				if s := d.Operation.Status + "/" + d.Operation.Phase; s != last {
					log.Printf("  %s: %s", name, s)
					last = s
				}
				if d.Operation.Status != "running" {
					return d
				}
			}
		}
		time.Sleep(10 * time.Second)
	}
	log.Fatalf("NG: %s の Workflow %s が %s で終わらない", name, runID, timeout)
	return deployment{}
}

// kube は Pod の ServiceAccount のトークンで Kubernetes の API を読む (ArgoCD の Application)。
func kube(path string) (int, map[string]any) {
	tok, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/token")
	if err != nil {
		log.Fatal(err)
	}
	ca, _ := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	c := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}}
	req, _ := http.NewRequest("GET", "https://kubernetes.default.svc"+path, nil)
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(tok)))
	r, err := c.Do(req)
	if err != nil {
		log.Fatal(err)
	}
	defer r.Body.Close()
	var m map[string]any
	_ = json.NewDecoder(r.Body).Decode(&m)
	return r.StatusCode, m
}

func field(m map[string]any, path ...string) string {
	var cur any = m
	for _, p := range path {
		mm, ok := cur.(map[string]any)
		if !ok {
			return ""
		}
		cur = mm[p]
	}
	s, _ := cur.(string)
	return s
}

// traceServices は Tempo の trace に出てくる service.name の集まり。
func traceServices(traceID string) []string {
	r, err := http.Get(tempo + "/api/traces/" + traceID)
	if err != nil || r.StatusCode != 200 {
		return nil
	}
	defer r.Body.Close()
	var t struct {
		Batches       []resourceSpans `json:"batches"`
		ResourceSpans []resourceSpans `json:"resourceSpans"`
	}
	_ = json.NewDecoder(r.Body).Decode(&t)
	var names []string
	for _, b := range append(t.Batches, t.ResourceSpans...) {
		for _, a := range b.Resource.Attributes {
			if a.Key == "service.name" && !slices.Contains(names, a.Value.StringValue) {
				names = append(names, a.Value.StringValue)
			}
		}
	}
	slices.Sort(names)
	return names
}

type resourceSpans struct {
	Resource struct {
		Attributes []struct {
			Key   string `json:"key"`
			Value struct {
				StringValue string `json:"stringValue"`
			} `json:"value"`
		} `json:"attributes"`
	} `json:"resource"`
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func main() {
	log.SetFlags(log.Ltime)
	name := env("APP_NAME", "e2e-"+time.Now().UTC().Format("0102-1504"))
	log.Printf("アプリ %s を %s 経由でデプロイして消す", name, base)

	// 1. 認証
	check(call("GET", "/api/v1/deployments", "", nil, nil).code == 401, "トークン無しは 401")
	check(call("GET", "/api/v1/deployments", "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln", nil, nil).code == 401, "不正なトークンは 401")
	token := clientToken()

	// 入力検証と 404
	bad := call("POST", "/api/v1/deployments", token, map[string]any{"name": "Bad_Name", "image": "nginx"}, nil)
	check(bad.code == 422 && bad.header.Get("Content-Type") == "application/problem+json", "不正な依頼は 422 の problem+json (%d)", bad.code)
	check(call("GET", "/api/v1/deployments/no-such-app-"+randomHex(3), token, nil, nil).code == 404, "無いアプリは 404")

	// 2. デプロイ (trace の ID はこの試験が決める。Tempo で引く)
	traceID := randomHex(16)
	app := map[string]any{"name": name, "image": image, "port": 8080, "replicas": 1}
	key := "e2e-" + randomHex(8)
	r := call("POST", "/api/v1/deployments", token, app, map[string]string{
		"Idempotency-Key": key, "traceparent": "00-" + traceID + "-" + randomHex(8) + "-01",
	})
	check(r.code == 202, "デプロイの依頼は 202 (%d %s)", r.code, r.body)
	var op operation
	r.json(&op)
	again := call("POST", "/api/v1/deployments", token, app, map[string]string{"Idempotency-Key": key})
	var op2 operation
	again.json(&op2)
	check(again.code == 202 && op2.RunID == op.RunID && again.header.Get("Idempotent-Replayed") == "true", "同じ Idempotency-Key は同じ run を返す")
	conflict := call("POST", "/api/v1/deployments", token, app, map[string]string{"Idempotency-Key": key + "-other"})
	check(conflict.code == 409, "走っている間の同じアプリの別の依頼は 409 (%d)", conflict.code)

	// 3. 完了まで
	d := waitDone(token, name, op.RunID)
	check(d.Operation.Status == "completed" && d.Last.RunID == op.RunID && d.Last.Result == "succeeded" && d.Deployed, "デプロイの Workflow が完了し履歴に succeeded (%+v)", d.Last)
	check(d.Last.PR != nil && strings.Contains(d.Last.PR.URL, "/pull/"), "PR ができた (%v)", d.Last.PR)
	code, appObj := kube("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/deploy-" + name)
	check(code == 200 && field(appObj, "status", "sync", "status") == "Synced" && field(appObj, "status", "health", "status") == "Healthy",
		"ApplicationSet が作った Application deploy-%s が Synced/Healthy (%d %s/%s)", name, code, field(appObj, "status", "sync", "status"), field(appObj, "status", "health", "status"))
	list := call("GET", "/api/v1/deployments?pageSize=100", token, nil, nil)
	var page struct {
		Items []deployment `json:"items"`
	}
	list.json(&page)
	found := slices.IndexFunc(page.Items, func(x deployment) bool { return x.Name == name && x.Deployed })
	check(found >= 0, "一覧 API (Dapr state) に deployed で出る")

	// 4. Tempo (取り込みに少しかかる)
	var got []string
	for range 24 {
		got = traceServices(traceID)
		if len(got) >= len(services) && !slices.ContainsFunc(services, func(s string) bool { return !slices.Contains(got, s) }) {
			break
		}
		time.Sleep(5 * time.Second)
	}
	missing := slices.DeleteFunc(slices.Clone(services), func(s string) bool { return slices.Contains(got, s) })
	check(len(missing) == 0, "Tempo の trace %s に 6 つの Pod のスパン (%v、足りない %v)", traceID, got, missing)

	// 5. 削除
	r = call("DELETE", "/api/v1/deployments/"+name, token, nil, map[string]string{"Idempotency-Key": "e2e-" + randomHex(8)})
	check(r.code == 202, "削除の依頼は 202 (%d %s)", r.code, r.body)
	var del operation
	r.json(&del)
	d = waitDone(token, name, del.RunID)
	check(d.Operation.Status == "completed" && d.Last.RunID == del.RunID && d.Last.Op == "delete" && d.Last.Result == "succeeded" && !d.Deployed, "削除の Workflow が完了 (%+v)", d.Last)
	code, _ = kube("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/deploy-" + name)
	check(code == 404, "Application deploy-%s が消えた (%d)", name, code)

	if err := os.WriteFile(out, []byte(fmt.Sprintf("APP=%s\nDEPLOY_RUN=%s\nDELETE_RUN=%s\n", name, op.RunID, del.RunID)), 0o644); err != nil {
		log.Fatal(err)
	}
	log.Printf("すべて OK (%s に run の ID を書いた)", out)
}
