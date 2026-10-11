"""gitops-deployer の、ファイルをまたぐ食い違いの試験 (just ci)。標準ライブラリだけ。

名前 (APISIX の経路の先・ApplicationSet のラベル・Kafka のトピック・イメージのタグ・Keycloak の client) は Go のコード・manifest・
基盤の設定・just のレシピに別々に書いてあり、1 つでもずれるとクラスタに入ってから初めて繋がらないと分かる。ここで突き合わせる。
設計は docs/cluster/gitops-deployer.md。
"""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
KIND = ROOT / "clusters/kind"
MANIFESTS = HERE / "manifests"


def read(path):
    return Path(path).read_text(encoding="utf-8")


def go_const(path, name):
    m = re.search(rf'\b{name}\s*=\s*"([^"]+)"', read(HERE / path))
    if not m:
        raise AssertionError(f"{path} に {name} が無い")
    return m.group(1)


CONFIG = read(MANIFESTS / "config.yaml")


def config(key):
    return re.search(rf"(?m)^  {key}: (.+)$", CONFIG).group(1).strip().strip('"')


class PodsTest(unittest.TestCase):
    PODS = sorted(p.name for p in (HERE / "cmd").iterdir() if p.is_dir())

    def test_six_pods_one_image(self):
        self.assertEqual(self.PODS, ["api", "bff", "event-receiver", "git-activity", "store-activity", "workflow-engine"])
        image = re.search(r'gitops_deployer_image := "(\S+)"', read(ROOT / "just/gitops-deployer.just")).group(1)
        images = set()
        for f in MANIFESTS.glob("*.yaml"):
            images |= set(re.findall(r"image: (kind\.local/\S+)", read(f)))
        self.assertEqual(images, {image})
        # Knative はタグを digest に解決しにいく。kind.local/ は飛ばされる (config-deployment の既定)
        self.assertTrue(image.startswith("kind.local/"))

    def test_each_cmd_runs_in_its_own_pod(self):
        text = "".join(read(f) for f in MANIFESTS.glob("*.yaml"))
        for name in self.PODS:
            self.assertEqual(text.count(f"command: [/usr/local/bin/{name}]"), 1, name)

    def test_each_pod_has_requests_and_a_memory_limit(self):
        # kube-linter の unset-*-requirements は .kube-linter.yaml で外してあるので、ここで確かめる。
        # 6 つの Pod の manifest (command の行から manifest の終わりまで) に requests の cpu・memory と limits の memory があること
        text = "".join(read(f) + "\n---\n" for f in MANIFESTS.glob("*.yaml"))
        for name in self.PODS:
            with self.subTest(pod=name):
                block = re.search(rf"command: \[/usr/local/bin/{name}\]\n(.*?)\n---", text, re.S).group(1)
                self.assertRegex(block, r"resources:\n\s+requests:\n\s+cpu: \S+\n\s+memory: \S+\n\s+limits:\n\s+memory: \S+")

    def test_dockerfile_go_matches_go_mod(self):
        go = re.search(r"(?m)^go (\d+\.\d+)", read(HERE / "go.mod")).group(1)
        self.assertRegex(read(HERE / "Dockerfile"), rf"FROM golang:{re.escape(go)}\.\d+-alpine")

    def test_only_git_activity_holds_the_github_app(self):
        holders = []
        for f in MANIFESTS.glob("*.yaml"):
            for doc in read(f).split("\n---\n"):
                if "github-app" in doc and re.search(r"(?m)^kind: (Deployment|Service)$", doc):
                    holders.append(re.search(r"(?m)^  name: (\S+)", doc).group(1))
        self.assertEqual(holders, ["git-activity"])

    def test_git_activity_reads_the_private_key_from_the_mounted_file(self):
        doc = [d for d in read(MANIFESTS / "workers.yaml").split("\n---\n") if "  name: git-activity\n" in d][0]
        path = re.search(r"GITHUB_APP_PRIVATE_KEY_FILE\n\s+value: (\S+)", doc).group(1)
        mount = re.search(r"- name: github-app\n\s+mountPath: (\S+)", doc).group(1)
        self.assertEqual(path, mount + "/private-key.pem")
        # 0440 を runAsUser のグループで読む
        self.assertIn("defaultMode: 0440", doc)
        self.assertIn("fsGroup: 65532", doc)
        # 既定の値 (cmd/git-activity) とも揃える
        self.assertIn(f'"GITHUB_APP_PRIVATE_KEY_FILE", "{path}"', read(HERE / "cmd/git-activity/main.go"))


class RouteTest(unittest.TestCase):
    ROUTES = read(KIND / "apisix/manifests/routes.yaml")
    ROUTE = ROUTES.split("- id: gitops-deployer\n")[1].split("#END")[0]

    def test_apisix_routes_to_the_bff_service(self):
        bff = read(MANIFESTS / "bff.yaml").split("kind: Service\n")[1]
        self.assertIn("name: bff\n  namespace: gitops-deployer", bff)
        self.assertRegex(bff, r"port: 80\n")
        self.assertIn("bff.gitops-deployer.svc.cluster.local:80: 1", self.ROUTE)

    def test_apisix_checks_the_same_issuer_and_audience_as_bff(self):
        self.assertIn(f"discovery: {config('OIDC_ISSUER')}/.well-known/openid-configuration", self.ROUTE)
        self.assertIn(f"client_id: {config('OIDC_AUDIENCE')}", self.ROUTE)
        self.assertIn("bearer_only: true", self.ROUTE)
        self.assertIn("limit-count:", self.ROUTE)
        realm = read(KIND / "auth/keycloak/realm-home-k8s.yaml")
        self.assertIn(f"      - clientId: {config('OIDC_AUDIENCE')}\n", realm)
        self.assertIn(f"included.client.audience: {config('OIDC_AUDIENCE')}", realm)

    def test_e2e_enters_through_the_gateway_and_the_route_prefix(self):
        prefix = re.search(r"uri: (/[^/]+)/api/\*", self.ROUTE).group(1)
        self.assertIn(f'"BASE_URL", "http://gateway-istio.istio-ingress.svc.cluster.local{prefix}"', read(HERE / "e2e/main.go"))
        self.assertIn('"^/gitops-deployer/(.*)", "/$1"', self.ROUTE)


class GitOpsTest(unittest.TestCase):
    APPSET = read(KIND / "argocd/apps/gitops-deployer-apps.yaml")

    def test_applicationset_label_is_what_the_receiver_and_the_source_read(self):
        label = go_const("internal/receiver/receiver.go", "LabelApp")
        self.assertIn(f"{label}: '{{{{.path.basename}}}}'", self.APPSET)
        self.assertIn(f"- key: {label}\n            operator: Exists", read(MANIFESTS / "eventing.yaml"))

    def test_applicationset_reads_the_directory_the_workflow_writes(self):
        root = go_const("internal/spec/spec.go", "Root")
        ns = go_const("internal/spec/spec.go", "Namespace")
        self.assertIn(f"- path: {root}/*", self.APPSET)
        self.assertIn(f"namespace: {ns}", self.APPSET)
        self.assertIn(f"name: {ns}\n", read(KIND / "gitops-deployer/namespaces.yaml"))
        self.assertIn(f'deploy-{{{{.path.basename}}}}', self.APPSET)
        self.assertIn('"/applications/argocd/deploy-"', read(HERE / "internal/bff/bff.go"))

    def test_argocd_names_match_the_receiver(self):
        self.assertIn('applications/deploy-" + name', read(HERE / "e2e/main.go"))


class EventsTest(unittest.TestCase):
    TOPICS = read(MANIFESTS / "kafka-topics.yaml")

    def test_completed_event_topic(self):
        topic = go_const("internal/history/history.go", "Topic")
        self.assertIn(f"name: {topic}\n", self.TOPICS)
        self.assertIn(f"--topic {topic} ", read(MANIFESTS / "e2e.yaml"))

    def test_dead_letter_goes_to_a_broker_without_triggers(self):
        eventing = read(MANIFESTS / "eventing.yaml")
        dls = re.search(r"deadLetterSink:\n      ref:\n(?:        .*\n)+", eventing).group(0)
        self.assertIn("kind: Broker", dls)
        name = re.search(r"name: (\S+)", dls).group(1)
        self.assertIn(f"kind: Broker\nmetadata:\n  name: {name}\n", eventing)
        self.assertNotIn(f"broker: {name}\n", eventing)

    def test_kafka_bootstrap_matches_the_platform(self):
        bootstrap = re.search(r"value: (kafka-kafka-bootstrap\S+)", read(KIND / "gitops-deployer/dapr-components.yaml")).group(1)
        self.assertIn(f"--bootstrap-server {bootstrap} ", read(MANIFESTS / "e2e.yaml"))


class DaprTest(unittest.TestCase):
    def test_state_is_shared_between_api_and_store_activity(self):
        # 既定の keyPrefix (app-id) では、store-activity の書いた履歴を api が読めない
        store = config("DAPR_STATESTORE")
        component = read(MANIFESTS / "dapr.yaml").split(f"kind: Component\nmetadata:\n  name: {store}\n")[1]
        self.assertRegex(component, r"- name: keyPrefix\n      value: name\n")
        self.assertIn("\nscopes:\n  - api\n  - store-activity", component)
        redis = re.search(r"value: (redis\S+)", read(KIND / "gitops-deployer/dapr-components.yaml")).group(1)
        self.assertIn(f"value: {redis}\n", component)
        self.assertIn(f"name: {config('DAPR_PUBSUB')}\n", read(KIND / "gitops-deployer/dapr-components.yaml"))

    def test_dapr_sidecars_and_app_ids(self):
        for name, f in [("bff", "bff.yaml"), ("api", "api.yaml"), ("store-activity", "workers.yaml")]:
            self.assertIn(f"dapr.io/app-id: {name}\n", read(MANIFESTS / f))
        # workflow-engine と git-activity は Dapr を使わない (合意書の Pod の構成)
        workers = read(MANIFESTS / "workers.yaml").split("\n---\n")
        for doc in workers:
            if "name: workflow-engine\n" in doc or "name: git-activity\n" in doc:
                self.assertNotIn("dapr.io/enabled", doc)
        self.assertIn('server.Env("API_APP_ID", "api")', read(HERE / "cmd/bff/main.go"))

    def test_only_bff_may_call_api(self):
        policy = read(MANIFESTS / "rbac.yaml").split("kind: AuthorizationPolicy\n")[1]
        self.assertIn("app.kubernetes.io/name: api", policy)
        self.assertIn("principals: [cluster.local/ns/gitops-deployer/sa/bff]", policy)
        self.assertIn("serviceAccountName: bff\n", read(MANIFESTS / "bff.yaml"))


class TemporalTest(unittest.TestCase):
    def test_temporal_address_is_the_dev_internal_frontend(self):
        self.assertEqual(config("TEMPORAL_ADDRESS"), "temporal-internal-frontend.dev.svc.cluster.local:7236")
        ns = config("TEMPORAL_NAMESPACE")
        self.assertIn(f"-n {ns} --retention", read(MANIFESTS / "temporal-namespace.yaml"))

    def test_task_queues(self):
        for const, pod in [("TaskQueue", "workflow-engine"), ("GitTaskQueue", "git-activity"), ("StoreTaskQueue", "store-activity")]:
            self.assertEqual(go_const("internal/workflow/types.go", const), pod)


if __name__ == "__main__":
    unittest.main()
