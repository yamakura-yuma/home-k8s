// git-activity: Task Queue git-activity の Activity (GitHub にブランチと PR を作り、auto-merge を有効にする)。internal/gitops
// 6 つの Pod のうち資格情報 (GitHub App の秘密鍵) を持つのはこの Pod だけ。
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.temporal.io/sdk/worker"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/gitops"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/temporalx"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

func main() {
	server.Main("git-activity", func(ctx context.Context) error {
		repo := server.Env("GITHUB_REPO", "home-k8s")
		app := &gitops.AppToken{
			API:            "https://api.github.com",
			AppID:          os.Getenv("GITHUB_APP_ID"),
			InstallationID: os.Getenv("GITHUB_APP_INSTALLATION_ID"),
			KeyFile:        server.Env("GITHUB_APP_PRIVATE_KEY_FILE", "/var/run/secrets/github-app/private-key.pem"),
			Repo:           repo,
			HTTP:           &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport), Timeout: 20 * time.Second},
		}
		if app.AppID == "" || app.InstallationID == "" {
			return errors.New("GITHUB_APP_ID・GITHUB_APP_INSTALLATION_ID が無い (Secret gitops-deployer/github-app。just up が ~/.config/home-k8s/gitops-deployer-github-app.env から作る)")
		}
		// 起動の時点で鍵と installation を確かめる (間違っていれば依頼を受ける前に CrashLoop で気づく)
		if _, err := app.Token(ctx); err != nil {
			return err
		}
		tc, err := temporalx.New()
		if err != nil {
			return err
		}
		defer tc.Close()
		w := worker.New(tc, workflow.GitTaskQueue, worker.Options{WorkerStopTimeout: 20 * time.Second})
		(&gitops.Activities{
			GH:         gitops.NewGitHub(server.Env("GITHUB_OWNER", "yamakura-yuma"), repo, server.Env("GITHUB_BASE", "main"), app),
			Registries: strings.Split(server.Env("IMAGE_REGISTRIES", "docker.io"), ","),
		}).Register(w)
		return server.RunWorker(ctx, "git-activity", w)
	})
}
