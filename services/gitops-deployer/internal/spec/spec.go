// Package spec はデプロイの依頼 (アプリの名前・イメージ・ポート・台数) の検証と、home-k8s の deploys/<名前>/ に置く
// マニフェストの生成。マニフェスト注入を防ぐため、値は正規表現と許可リストで絞ったうえで、文字列の連結ではなく
// 構造体 (map) を YAML に直列化して作る。置き場所は検証済みの名前からしか作らない。
package spec

import (
	"fmt"
	"regexp"
	"slices"
	"strings"

	"sigs.k8s.io/yaml"
)

type Op string

const (
	OpDeploy Op = "deploy"
	OpDelete Op = "delete"
)

// App はデプロイの依頼。Backstage の Scaffolder のフォームと同じ欄。
type App struct {
	Name     string `json:"name"`
	Image    string `json:"image"`
	Port     int    `json:"port"`
	Replicas int    `json:"replicas"`
}

// Namespace はデプロイされるアプリの置き場 (合意書: 共通の namespace)。
const Namespace = "gitops-deployer-apps"

// Root は Workflow が作る PR が触ってよい唯一のディレクトリ。
const Root = "deploys"

// FieldError は入力の誤り 1 つ (problem+json の errors に入る)。
type FieldError struct {
	Field  string `json:"field"`
	Detail string `json:"detail"`
}

type Errors []FieldError

func (e Errors) Error() string {
	parts := make([]string, len(e))
	for i, f := range e {
		parts[i] = f.Field + ": " + f.Detail
	}
	return strings.Join(parts, "; ")
}

var (
	// DNS-1123 label。Application の名前 deploy-<名前> と Deployment・Service の名前になるので 30 文字まで
	nameRe = regexp.MustCompile(`^[a-z]([a-z0-9-]{0,28}[a-z0-9])?$`)
	// <レジストリ>/<パス>:<タグ> か @sha256:<digest>。レジストリは明示させる (docker.io を暗黙にしない)。latest は不可 (下で弾く)
	imageRe = regexp.MustCompile(`^([a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?)/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})$`)
)

// ValidateName は削除の依頼の名前だけを確かめる。
func ValidateName(name string) error {
	if !nameRe.MatchString(name) {
		return Errors{{Field: "name", Detail: "英小文字・数字・- の 1〜30 文字で、英小文字で始まり英小文字か数字で終わる"}}
	}
	return nil
}

// Normalize は省略された欄に既定値を入れる (port 8080・replicas 1)。
func (a App) Normalize() App {
	if a.Port == 0 {
		a.Port = 8080
	}
	if a.Replicas == 0 {
		a.Replicas = 1
	}
	return a
}

// Validate は依頼を確かめる。registries はイメージのレジストリの許可リスト (設定 IMAGE_REGISTRIES)。
func (a App) Validate(registries []string) error {
	var errs Errors
	if err := ValidateName(a.Name); err != nil {
		errs = append(errs, err.(Errors)...)
	}
	switch m := imageRe.FindStringSubmatch(a.Image); {
	case len(a.Image) > 255 || m == nil:
		errs = append(errs, FieldError{"image", "<レジストリ>/<リポジトリ>:<タグ> の形 (レジストリとタグは省略しない)"})
	case strings.HasSuffix(a.Image, ":latest"):
		errs = append(errs, FieldError{"image", "タグ latest は使わない (どの版が動くかが Git から分からない)"})
	case !slices.Contains(registries, m[1]):
		errs = append(errs, FieldError{"image", fmt.Sprintf("レジストリ %s は許可されていない (%s)", m[1], strings.Join(registries, ", "))})
	}
	if a.Port < 1024 || a.Port > 65535 {
		errs = append(errs, FieldError{"port", "1024〜65535 (root でないコンテナが開けるポート)"})
	}
	if a.Replicas < 1 || a.Replicas > 3 {
		errs = append(errs, FieldError{"replicas", "1〜3"})
	}
	if errs != nil {
		return errs
	}
	return nil
}

// Dir は deploys/<名前>。ValidateName を通った名前にだけ使う。
func Dir(name string) string { return Root + "/" + name }

// File は 1 アプリのマニフェストのファイル (Deployment と Service)。
func File(name string) string { return Dir(name) + "/app.yaml" }

// Render は Validate を通った依頼から、deploys/<名前>/app.yaml の中身を作る。
// コンテナは root で動かさず、ルートファイルシステムは読み取りだけ (書き込みは /tmp の emptyDir)。kube-linter の既定の検査を通る形。
func Render(a App) ([]byte, error) {
	labels := map[string]any{
		"app.kubernetes.io/name":       a.Name,
		"app.kubernetes.io/managed-by": "gitops-deployer",
	}
	selector := map[string]any{"app.kubernetes.io/name": a.Name}
	probe := map[string]any{"tcpSocket": map[string]any{"port": "http"}, "periodSeconds": 10}
	deployment := map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata":   map[string]any{"name": a.Name, "namespace": Namespace, "labels": labels},
		"spec": map[string]any{
			"replicas": a.Replicas,
			"selector": map[string]any{"matchLabels": selector},
			"template": map[string]any{
				"metadata": map[string]any{"labels": labels},
				"spec": map[string]any{
					"automountServiceAccountToken": false,
					"securityContext": map[string]any{
						"runAsNonRoot":   true,
						"seccompProfile": map[string]any{"type": "RuntimeDefault"},
					},
					// 台数が 2 以上のときにノードを散らす (kube-linter の no-anti-affinity)
					"affinity": map[string]any{"podAntiAffinity": map[string]any{
						"preferredDuringSchedulingIgnoredDuringExecution": []any{map[string]any{
							"weight": 100,
							"podAffinityTerm": map[string]any{
								"topologyKey":   "kubernetes.io/hostname",
								"labelSelector": map[string]any{"matchLabels": selector},
							},
						}},
					}},
					"containers": []any{map[string]any{
						"name":  "app",
						"image": a.Image,
						"ports": []any{map[string]any{"name": "http", "containerPort": a.Port}},
						"securityContext": map[string]any{
							"allowPrivilegeEscalation": false,
							"readOnlyRootFilesystem":   true,
							"capabilities":             map[string]any{"drop": []any{"ALL"}},
						},
						"resources": map[string]any{
							"requests": map[string]any{"cpu": "10m", "memory": "32Mi"},
							"limits":   map[string]any{"memory": "128Mi"},
						},
						"readinessProbe": probe,
						"livenessProbe":  probe,
						"volumeMounts":   []any{map[string]any{"name": "tmp", "mountPath": "/tmp"}},
					}},
					"volumes": []any{map[string]any{"name": "tmp", "emptyDir": map[string]any{}}},
				},
			},
		},
	}
	service := map[string]any{
		"apiVersion": "v1",
		"kind":       "Service",
		"metadata":   map[string]any{"name": a.Name, "namespace": Namespace, "labels": labels},
		"spec": map[string]any{
			"selector": selector,
			"ports":    []any{map[string]any{"name": "http", "port": 80, "targetPort": "http"}},
		},
	}
	var b strings.Builder
	b.WriteString("# gitops-deployer が生成した (手で直さない)。ApplicationSet gitops-deployer-apps が Application deploy-" + a.Name + " にする\n")
	for i, obj := range []any{deployment, service} {
		y, err := yaml.Marshal(obj)
		if err != nil {
			return nil, err
		}
		if i > 0 {
			b.WriteString("---\n")
		}
		b.Write(y)
	}
	return []byte(b.String()), nil
}
