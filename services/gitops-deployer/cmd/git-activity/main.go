// git-activity: Task Queue git-activity の Activity (GitHub にブランチと PR を作り、auto-merge を有効にする)。internal/gitops
// 6 つの Pod のうち資格情報 (GitHub の fine-grained PAT) を持つのはこの Pod だけ。
package main

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"go.temporal.io/sdk/worker"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/gitops"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/temporalx"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

func main() {
	server.Main("git-activity", func(ctx context.Context) error {
		token := os.Getenv("GITHUB_TOKEN")
		if token == "" {
			return errors.New("GITHUB_TOKEN が無い (Secret gitops-deployer/github-token。just up が ~/.config/home-k8s/gitops-deployer-github.env から作る)")
		}
		tc, err := temporalx.New()
		if err != nil {
			return err
		}
		defer tc.Close()
		w := worker.New(tc, workflow.GitTaskQueue, worker.Options{WorkerStopTimeout: 20 * time.Second})
		(&gitops.Activities{
			GH:         gitops.NewGitHub(server.Env("GITHUB_OWNER", "yamakura-yuma"), server.Env("GITHUB_REPO", "home-k8s"), server.Env("GITHUB_BASE", "main"), token),
			Registries: strings.Split(server.Env("IMAGE_REGISTRIES", "docker.io"), ","),
		}).Register(w)
		return server.RunWorker(ctx, "git-activity", w)
	})
}
