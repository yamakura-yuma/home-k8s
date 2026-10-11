"""gitops-deployer の基盤 (Istio ambient・Knative・Kafka・Redis・Dapr・APISIX・testkube) の、ファイルをまたぐ食い違いの試験 (just ci)。標準ライブラリだけ。

Kafka の入口の名前・Gateway の Service の名前・APISIX の経路は、別々の Application の manifest に書いてあり、1 つでもずれると
クラスタに入ってから初めて繋がらないと分かる。ambient に入れる namespace も、合意 (既存の namespace は入れない) から外れていないかをここで見る。
設計は docs/cluster/gitops-deployer-platform.md。
"""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

HERE = Path(__file__).resolve().parent
KIND = HERE.parent
ROOT = KIND.parent.parent
APPS = KIND / "argocd/apps"


def read(path):
    return (KIND / path).read_text(encoding="utf-8")


# ambient に入れてよい namespace。gitops-deployer は合意書、redis・apisix は後段への通信を mTLS で包むため。
# 既存の namespace (観測スタック・Temporal・Keycloak・share・Backstage など) は入れない (合意書)
AMBIENT_NAMESPACES = {"gitops-deployer", "redis", "apisix"}


class AmbientTest(unittest.TestCase):
    def test_only_the_new_namespaces_are_enrolled_in_ambient(self):
        enrolled = set()
        for path in KIND.rglob("*.yaml"):
            text = path.read_text(encoding="utf-8")
            for doc in text.split("\n---"):
                if "istio.io/dataplane-mode: ambient" in doc:
                    m = re.search(r"^kind: Namespace\nmetadata:\n  name: (\S+)", doc, re.M)
                    self.assertIsNotNone(m, f"Namespace 以外に ambient のラベルがある: {path}")
                    enrolled.add(m.group(1))
        self.assertEqual(enrolled, AMBIENT_NAMESPACES)

    def test_istio_charts_share_one_version_and_the_ambient_profile(self):
        versions = {
            app: re.search(r"targetRevision: (\d+\.\d+\.\d+)", (APPS / f"{app}.yaml").read_text(encoding="utf-8")).group(1)
            for app in ("istio-base", "istiod", "istio-cni", "ztunnel")
        }
        self.assertEqual(len(set(versions.values())), 1, versions)
        for values in ("istio/istiod-values.yaml", "istio/cni-values.yaml"):
            self.assertRegex(read(values), r"(?m)^profile: ambient$", values)


class GatewayTest(unittest.TestCase):
    GATEWAY = read("istio/gateway/gateway.yaml")

    def gateways(self):
        return set(re.findall(r"kind: Gateway\nmetadata:\n  name: (\S+)\n  namespace: istio-ingress", self.GATEWAY))

    def test_gateways_are_cluster_ip(self):
        # kind に LoadBalancer は無い。LoadBalancer のままだと Service に住所が付かず、Gateway が Programmed にならない
        self.assertEqual(self.GATEWAY.count("networking.istio.io/service-type: ClusterIP"), len(self.gateways()))

    def test_knative_points_at_existing_gateways_and_their_services(self):
        serving = read("knative/serving/kustomization.yaml")
        self.assertIn("ingress-class: gateway-api.ingress.networking.knative.dev", serving)
        pairs = re.findall(r"gateway: istio-ingress/(\S+)\n\s+service: istio-ingress/(\S+)", serving)
        self.assertEqual({g for g, _ in pairs}, self.gateways())
        for gateway, service in pairs:
            # istiod が Gateway ごとに作る Service の名前は <Gateway 名>-istio
            self.assertEqual(service, f"{gateway}-istio")

    def test_external_gateway_admits_only_labelled_namespaces(self):
        labelled = {
            m.group(1)
            for path in KIND.rglob("*.yaml")
            for m in re.finditer(r"kind: Namespace\nmetadata:\n  name: (\S+)\n  labels:\n(?:    .*\n)*?    home-k8s/gateway-access: \"true\"",
                                 path.read_text(encoding="utf-8"))
        }
        self.assertEqual(labelled, {"apisix", "gitops-deployer"})
        self.assertIn("from: Selector", self.GATEWAY.split("name: knative-local-gateway")[0])


class KafkaTest(unittest.TestCase):
    def bootstrap(self):
        kafka = read("kafka/kafka.yaml")
        name = re.search(r"kind: Kafka\nmetadata:\n  name: (\S+)\n  namespace: (\S+)", kafka)
        port = re.search(r"- name: plain\n\s+port: (\d+)\n\s+type: internal\n\s+tls: false", kafka)
        self.assertIsNotNone(name)
        self.assertIsNotNone(port)
        # Strimzi が作る Service は <Kafka 名>-kafka-bootstrap
        return f"{name.group(1)}-kafka-bootstrap.{name.group(2)}.svc", port.group(1)

    def test_knative_broker_uses_the_strimzi_bootstrap(self):
        host, port = self.bootstrap()
        self.assertIn(f"bootstrap.servers: {host}:{port}", read("knative/eventing/kustomization.yaml"))

    def test_dapr_pubsub_uses_the_strimzi_bootstrap(self):
        host, port = self.bootstrap()
        self.assertIn(f"value: {host}.cluster.local:{port}", read("gitops-deployer/dapr-components.yaml"))

    def test_single_broker_keeps_replication_at_one(self):
        self.assertIn("replicas: 1", read("kafka/kafka.yaml"))
        self.assertIn('default.topic.replication.factor: "1"', read("knative/eventing/kustomization.yaml"))


class DaprTest(unittest.TestCase):
    def test_mtls_is_disabled(self):
        # 暗号化は ambient に任せる (合意書)
        self.assertRegex(read("dapr/values.yaml"), r"(?m)^global:\n  mtls:\n    enabled: false$")

    def test_statestore_points_at_the_redis_service(self):
        redis = read("redis/redis.yaml")
        self.assertRegex(redis, r"kind: Service\nmetadata:\n  name: redis\n  namespace: redis\n(?:.*\n)*?    - name: tcp-redis\n      port: 6379")
        self.assertIn("value: redis.redis.svc.cluster.local:6379", read("gitops-deployer/dapr-components.yaml"))

    def test_redis_admits_only_gitops_deployer(self):
        policy = read("redis/redis.yaml").split("kind: AuthorizationPolicy")[1]
        self.assertIn("action: ALLOW", policy)
        self.assertEqual(re.findall(r"namespaces:\n\s+- (\S+)", policy), ["gitops-deployer"])


class ApisixTest(unittest.TestCase):
    ROUTES = read("apisix/manifests/routes.yaml")

    def test_values_read_the_routes_config_map(self):
        name = re.search(r"kind: ConfigMap\nmetadata:\n  name: (\S+)", self.ROUTES).group(1)
        self.assertIn(f"existingConfigMap: {name}", read("apisix/values.yaml"))
        self.assertIn("mode: standalone", read("apisix/values.yaml"))

    def test_routes_end_with_the_end_marker(self):
        # standalone の apisix.yaml は末尾の #END が無いと読み込まれない
        self.assertTrue(self.ROUTES.rstrip().endswith("#END"))

    def test_smoke_test_hits_the_gateway_service_and_the_ping_route(self):
        smoke = read("testkube/manifests/platform-smoke.yaml")
        uri = re.search(r"uri: (\S+)", self.ROUTES).group(1)
        self.assertIn(f"http://gateway-istio.istio-ingress.svc.cluster.local{uri}", smoke)
        httproute = read("apisix/manifests/httproute.yaml")
        self.assertIn("name: gateway\n      namespace: istio-ingress", httproute)
        self.assertIn("name: apisix-gateway", httproute)


if __name__ == "__main__":
    unittest.main()
