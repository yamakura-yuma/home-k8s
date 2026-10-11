package spec

import (
	"errors"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"
)

var registries = []string{"docker.io", "ghcr.io"}

func valid() App {
	return App{Name: "hello", Image: "docker.io/nginxinc/nginx-unprivileged:1.29-alpine", Port: 8080, Replicas: 1}
}

func fields(err error) []string {
	var errs Errors
	if !errors.As(err, &errs) {
		return nil
	}
	var out []string
	for _, e := range errs {
		out = append(out, e.Field)
	}
	return out
}

func TestValidateAccepts(t *testing.T) {
	for _, a := range []App{
		valid(),
		{Name: "a", Image: "ghcr.io/org/team/app@sha256:" + strings.Repeat("a", 64), Port: 1024, Replicas: 3},
	} {
		if err := a.Validate(registries); err != nil {
			t.Errorf("%+v: %v", a, err)
		}
	}
}

func TestValidateRejects(t *testing.T) {
	cases := map[string]struct {
		mutate func(*App)
		field  string
	}{
		"大文字の名前":         {func(a *App) { a.Name = "Hello" }, "name"},
		"- で終わる名前":       {func(a *App) { a.Name = "hello-" }, "name"},
		"長すぎる名前":         {func(a *App) { a.Name = strings.Repeat("a", 31) }, "name"},
		"パスを含む名前":        {func(a *App) { a.Name = "../etc" }, "name"},
		"改行で YAML を足す名前":  {func(a *App) { a.Name = "x\n  namespace: kube-system" }, "name"},
		"レジストリの無いイメージ":   {func(a *App) { a.Image = "nginx:1.29" }, "image"},
		"タグの無いイメージ":      {func(a *App) { a.Image = "docker.io/library/nginx" }, "image"},
		"latest":         {func(a *App) { a.Image = "docker.io/library/nginx:latest" }, "image"},
		"許可されないレジストリ":    {func(a *App) { a.Image = "evil.example.com/x:1" }, "image"},
		"改行を含むイメージ":      {func(a *App) { a.Image = "docker.io/x:1\n    command: [sh]" }, "image"},
		"特権ポート":          {func(a *App) { a.Port = 80 }, "port"},
		"台数が多すぎる":        {func(a *App) { a.Replicas = 4 }, "replicas"},
		"台数が負":           {func(a *App) { a.Replicas = -1 }, "replicas"},
	}
	for name, c := range cases {
		a := valid()
		c.mutate(&a)
		got := fields(a.Validate(registries))
		if len(got) != 1 || got[0] != c.field {
			t.Errorf("%s: 欄 %s で弾くはず、実際 %v", name, c.field, got)
		}
	}
}

func TestNormalizeDefaults(t *testing.T) {
	a := App{Name: "x", Image: "docker.io/a/b:1"}.Normalize()
	if a.Port != 8080 || a.Replicas != 1 {
		t.Fatalf("%+v", a)
	}
}

func TestRenderStaysInTheAppNamespaceAndDirectory(t *testing.T) {
	a := valid()
	a.Replicas = 2
	out, err := Render(a)
	if err != nil {
		t.Fatal(err)
	}
	docs := strings.Split(string(out), "\n---\n")
	if len(docs) != 2 {
		t.Fatalf("Deployment と Service の 2 つ: %d", len(docs))
	}
	var kinds []string
	for _, d := range docs {
		var obj struct {
			Kind     string `json:"kind"`
			Metadata struct {
				Name      string `json:"name"`
				Namespace string `json:"namespace"`
			} `json:"metadata"`
		}
		if err := yaml.Unmarshal([]byte(d), &obj); err != nil {
			t.Fatal(err)
		}
		if obj.Metadata.Name != "hello" || obj.Metadata.Namespace != Namespace {
			t.Errorf("%s: %+v", obj.Kind, obj.Metadata)
		}
		kinds = append(kinds, obj.Kind)
	}
	if strings.Join(kinds, ",") != "Deployment,Service" {
		t.Errorf("%v", kinds)
	}
	var dep struct {
		Spec struct {
			Replicas int `json:"replicas"`
			Template struct {
				Spec struct {
					SecurityContext struct {
						RunAsNonRoot bool `json:"runAsNonRoot"`
					} `json:"securityContext"`
					Containers []struct {
						Image           string `json:"image"`
						SecurityContext struct {
							ReadOnlyRootFilesystem bool `json:"readOnlyRootFilesystem"`
						} `json:"securityContext"`
					} `json:"containers"`
				} `json:"spec"`
			} `json:"template"`
		} `json:"spec"`
	}
	if err := yaml.Unmarshal([]byte(docs[0]), &dep); err != nil {
		t.Fatal(err)
	}
	c := dep.Spec.Template.Spec.Containers[0]
	if dep.Spec.Replicas != 2 || c.Image != a.Image || !c.SecurityContext.ReadOnlyRootFilesystem || !dep.Spec.Template.Spec.SecurityContext.RunAsNonRoot {
		t.Errorf("%+v", dep)
	}
	if File("hello") != "deploys/hello/app.yaml" {
		t.Error(File("hello"))
	}
}
