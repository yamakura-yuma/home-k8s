"""kustomization.yaml の files がダッシュボードの JSON と揃っているかのテスト。標準ライブラリだけ:
    python3 -B -m unittest discover -s clusters/kind/observability/dashboards -p test_kustomization.py

generate.py や fetch.py で JSON を足したのに kustomization.yaml に書き忘れると、
ArgoCD は ConfigMap に入れず、Grafana に出ないまま気付けない。
"""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
HERE = Path(__file__).resolve().parent

# provider ごとの JSON の置き場所 (grafana-values.yaml の dashboardsConfigMaps と同じ群)
GROUPS = {
    "grafana-dashboards-default": "*.json",
    "grafana-dashboards-settings": "settings/*.json",
    "grafana-dashboards-grafana-com-kubernetes": "grafana-com/kubernetes/*.json",
    "grafana-dashboards-grafana-com-opentelemetry": "grafana-com/opentelemetry/*.json",
    "grafana-dashboards-grafana-com-claude-code": "grafana-com/claude-code/*.json",
}


def listed():
    """kustomization.yaml の ConfigMap ごとの files を返す (YAML の読み込みを使わず行で読む)"""
    groups, name = {}, None
    for line in (HERE / "kustomization.yaml").read_text().splitlines():
        if m := re.match(r"  - name: (\S+)$", line):
            name = m.group(1)
            groups[name] = []
        elif m := re.match(r"      - (\S+)$", line):
            groups[name].append(m.group(1))
    return groups


class Kustomization(unittest.TestCase):
    def test_files_match_json(self):
        want = {name: sorted(str(p.relative_to(HERE)) for p in HERE.glob(pat)) for name, pat in GROUPS.items()}
        self.assertEqual(listed(), want)


if __name__ == "__main__":
    unittest.main()
