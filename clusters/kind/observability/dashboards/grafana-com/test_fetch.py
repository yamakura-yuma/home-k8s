"""fetch.py の正規化 (grafana.com に触らずに確かめられる部分) のテスト。標準ライブラリだけ:
    python3 -B -m unittest discover -s clusters/kind/observability/dashboards/grafana-com
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch

ENTRY = {"id": 25255, "rev": 10, "url": "https://grafana.com/grafana/dashboards/25255", "uid": "gcom-fallback"}

# grafana.com の書き出し形式の最小形 (25255 と dotdc の形を混ぜたもの)
UPSTREAM = {
    "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "pluginId": "prometheus"}],
    "__elements": {},
    "__requires": [{"type": "grafana", "id": "grafana", "version": "11.0.0"}],
    "id": 7,
    "uid": "upstream-uid",
    "title": "t",
    "description": "Upstream description.",
    "panels": [{"datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
                "targets": [{"datasource": {"uid": "${DS_PROMETHEUS}"}, "expr": "sum(rate(x[$__rate_interval]))"}]}],
    "templating": {"list": [
        {"name": "datasource", "type": "datasource", "query": "prometheus", "current": {"text": "", "value": ""}},
        {"name": "cluster", "type": "query", "datasource": {"type": "prometheus", "uid": "${datasource}"},
         "query": "label_values(kube_node_info, cluster)"},
    ]},
}


def normalize(d=None, entry=ENTRY):
    return fetch.normalize(copy.deepcopy(d or UPSTREAM), entry)


class Normalize(unittest.TestCase):
    def test_drops_export_only_keys(self):
        got = normalize()
        for k in ("__inputs", "__requires", "__elements"):
            self.assertNotIn(k, got)

    def test_id_is_null_and_uid_is_upstream(self):
        got = normalize()
        self.assertIsNone(got["id"])
        self.assertEqual(got["uid"], "upstream-uid")

    def test_empty_upstream_uid_falls_back_to_manifest(self):
        # 25255 は uid が "" で配られている。Grafana を永続化していないので固定の uid が要る
        got = normalize(dict(UPSTREAM, uid=""))
        self.assertEqual(got["uid"], "gcom-fallback")

    def test_ds_inputs_become_literal_uid(self):
        got = normalize()
        self.assertEqual(got["panels"][0]["datasource"]["uid"], "prometheus")
        self.assertEqual(got["panels"][0]["targets"][0]["datasource"]["uid"], "prometheus")
        self.assertNotIn("${DS_", json.dumps(got))

    def test_dashboard_variables_are_left_alone(self):
        # ${datasource} はダッシュボードの変数で、Grafana が表示時に解決する
        got = normalize()
        self.assertEqual(got["templating"]["list"][1]["datasource"]["uid"], "${datasource}")
        self.assertIn("$__rate_interval", got["panels"][0]["targets"][0]["expr"])

    def test_datasource_variable_selects_prometheus(self):
        got = normalize()
        self.assertEqual(got["templating"]["list"][0]["current"], {"text": "Prometheus", "value": "prometheus"})

    def test_description_gets_source_link(self):
        self.assertEqual(normalize()["description"],
                         "Upstream description.\n\nSource: https://grafana.com/grafana/dashboards/25255 (rev 10)")
        no_desc = {k: v for k, v in UPSTREAM.items() if k != "description"}
        self.assertEqual(normalize(no_desc)["description"],
                         "Source: https://grafana.com/grafana/dashboards/25255 (rev 10)")

    def test_is_idempotent(self):
        # 同じ rev を取り直しても差分が出ない (fetch.py を打ち直しても git diff が空)
        self.assertEqual(json.dumps(normalize()), json.dumps(normalize()))

    def test_rejects_library_panels_and_non_datasource_inputs(self):
        with self.assertRaises(ValueError):
            normalize(dict(UPSTREAM, __elements={"abc": {"name": "lib"}}))
        with self.assertRaises(ValueError):
            normalize(dict(UPSTREAM, __inputs=[{"name": "VAR_X", "type": "constant", "value": "1"}]))


class Committed(unittest.TestCase):
    """コミット済みの JSON が manifest と揃っているか (取り直しを忘れて rev だけ上げた、などを拾う)。"""

    def test_every_manifest_entry_has_a_normalized_json(self):
        manifest = json.loads((HERE / "manifest.json").read_text())
        for group, g in manifest.items():
            names = {p.stem for p in (HERE / group).glob("*.json")}
            self.assertEqual(names, set(g["dashboards"]), group)
            for name, entry in g["dashboards"].items():
                d = json.loads((HERE / group / f"{name}.json").read_text())
                self.assertTrue(d["description"].endswith(f"Source: {entry['url']} (rev {entry['rev']})"), name)
                self.assertEqual(entry["url"], f"https://grafana.com/grafana/dashboards/{entry['id']}")
                self.assertIsNone(d["id"])
                self.assertTrue(d["uid"])
                self.assertNotIn("__inputs", d)


if __name__ == "__main__":
    unittest.main()
