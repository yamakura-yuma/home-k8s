"""Temporal (環境ごとの UI と Backstage のタブ) の、ファイルをまたぐ食い違いの試験 (just ci)。標準ライブラリだけ。

タブが出す URL (catalog-info.yaml の注釈) は、ホストのポート (kind-config)・NodePort (overlays)・Backstage の CSP の frame-src・
proxy の frame-ancestors と、1 つでもずれると画面が出ない (iframe が拒まれる)。クラスタには出ないので、ここで突き合わせる。
"""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
CADDYFILE = (HERE / "base" / "Caddyfile").read_text(encoding="utf-8")
CATALOG = (ROOT / "services/sample-api/catalog-info.yaml").read_text(encoding="utf-8")
APP_CONFIG = (ROOT / "backstage/app-config.yaml").read_text(encoding="utf-8")
KIND_CONFIG = (ROOT / "clusters/kind/kind-config.yaml").read_text(encoding="utf-8")
APPS = ROOT / "clusters/kind/argocd/apps"

# 環境 → (ホストのポート, NodePort)。docs/cluster/environments.md の予約と同じ
PORTS = {"dev": (8233, 30233), "prod": (8234, 30234)}
GRAFANA_PORTS = {3001: 30301, 3002: 30302}


class TemporalUrlTest(unittest.TestCase):
    def test_annotation_points_at_the_reserved_host_port_of_each_environment(self):
        for env, (host, _) in PORTS.items():
            self.assertIn(f"home-k8s/env.{env}.temporal-url: http://localhost:{host}\n", CATALOG)

    def test_kind_config_maps_each_host_port_to_the_node_port(self):
        mappings = dict(
            (int(host), int(container))
            for container, host in re.findall(r"containerPort: (\d+).*\n\s+hostPort: (\d+)", KIND_CONFIG)
        )
        for host, node in {**dict(PORTS.values()), **GRAFANA_PORTS}.items():
            self.assertEqual(mappings.get(host), node, f"hostPort {host}")

    def test_overlay_node_port_matches_the_mapping(self):
        for env, (_, node) in PORTS.items():
            text = (HERE / "overlays" / env / "kustomization.yaml").read_text(encoding="utf-8")
            self.assertRegex(text, rf"value: {node}\b", env)

    def test_csp_frame_src_allows_exactly_the_environment_origins(self):
        m = re.search(r"^\s+frame-src: \[(.*)\]", APP_CONFIG, re.M)
        self.assertIsNotNone(m, "backend.csp.frame-src が app-config.yaml に無い")
        origins = set(re.findall(r"'(http://[^']+)'", m.group(1)))
        self.assertEqual(origins, {f"http://localhost:{h}" for h, _ in PORTS.values()})


class EmbedProxyTest(unittest.TestCase):
    def test_x_frame_options_is_removed(self):
        self.assertIn("header_down -X-Frame-Options", CADDYFILE)

    def test_frame_ancestors_allows_backstage_origin_and_self_only(self):
        # Web UI は自分の /render を iframe に入れる (親が Backstage と Web UI の 2 段) ので 'self' が要る
        m = re.search(r'header_down Content-Security-Policy "frame-ancestors ([^"]*)"', CADDYFILE)
        self.assertIsNotNone(m)
        base_url = re.search(r"^  baseUrl: (http://\S+)", APP_CONFIG, re.M).group(1)
        self.assertEqual(sorted(m.group(1).split()), sorted(["'self'", base_url]))

    def test_proxy_targets_the_chart_web_service(self):
        # release 名 temporal の chart の Web UI の Service (apps/temporal.yaml の releaseName)
        self.assertIn("reverse_proxy temporal-web:8080", CADDYFILE)
        self.assertIn("releaseName: temporal\n", (APPS / "temporal.yaml").read_text(encoding="utf-8"))


class ApplicationSetTest(unittest.TestCase):
    def test_environments_match_the_catalog_annotation(self):
        envs = re.search(r"home-k8s/environments: (\S+)", CATALOG).group(1).split(",")
        for name in ("temporal", "temporal-support"):
            text = (APPS / f"{name}.yaml").read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"- env: (\w+)", text), envs, name)

    def test_db_is_not_in_the_chart_application(self):
        # chart の schema ジョブは PreSync で、DB が同じ Application にあると DB より先に走って待ち続ける
        text = (APPS / "temporal.yaml").read_text(encoding="utf-8")
        self.assertNotIn("clusters/kind/temporal/overlays", text)
        self.assertIn("clusters/kind/temporal/overlays/{{.env}}", (APPS / "temporal-support.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
