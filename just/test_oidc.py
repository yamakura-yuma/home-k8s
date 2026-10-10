"""ArgoCD と Grafana ×3 の OIDC (Keycloak) の試験。Secret を作るスクリプト (oidc-secrets.sh) と、各 UI の values と realm の突き合わせ。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_oidc.py
スクリプトは PATH の先頭に置いた偽の kubectl (fake_kubectl.py) で、引数と標準入力だけを見る。稼働中のクラスタには触れない。
values と realm は YAML を読まず、行で見る (test_keycloak.py と同じ)。
"""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_share_secrets import FakeEnv, decode  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REALM = (ROOT / "clusters/kind/auth/keycloak/realm-home-k8s.yaml").read_text()
ISSUER = "https://keycloak.taild2b611.ts.net/realms/home-k8s"
# <namespace>/<Secret> → (キー, clientId)
SECRETS = {
    "argocd/argocd-oidc-keycloak": ("clientSecret", "argocd"),
    "observability/grafana-oidc": ("client-secret", "grafana"),
    "dev/grafana-oidc": ("client-secret", "grafana-dev"),
    "prod/grafana-oidc": ("client-secret", "grafana-prod"),
}


def realm_client(client_id):
    """realm の宣言の client 1 つぶんの行。"""
    for block in re.split(r"\n      - clientId: ", REALM)[1:]:
        if block.split("\n", 1)[0] == client_id:
            return block.split("\n      # ", 1)[0]
    raise AssertionError(f"realm に client {client_id} が無い")


class OidcSecrets(FakeEnv):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "clients"
        self.dir.mkdir()
        for _, client in SECRETS.values():
            (self.dir / client).write_text(f"SECRET-{client}")

    def run_oidc(self, **env):
        return self.run_script("oidc-secrets.sh", str(self.dir), "kind-test", **env)

    def made(self):
        return {f'{e["manifest"]["metadata"]["namespace"]}/{e["manifest"]["metadata"]["name"]}': e
                for e in self.entries() if e["manifest"].get("kind") == "Secret"}

    def test_makes_one_secret_per_ui_from_the_client_files(self):
        r = self.run_oidc()
        self.assertEqual(r.returncode, 0, r.stderr)
        made = self.made()
        self.assertEqual(sorted(made), sorted(SECRETS))
        for name, (key, client) in SECRETS.items():
            self.assertEqual(made[name]["verb"], "create")
            self.assertEqual(decode(made[name]["manifest"]), {key: f"SECRET-{client}"}, name)
            self.assertNotIn("annotations", made[name]["manifest"]["metadata"], "apply の注釈を残さない (#49)")

    def test_argocd_secret_has_the_label_argocd_reads(self):
        # ArgoCD は oidc.config の $<Secret>:<キー> を、ラベル app.kubernetes.io/part-of: argocd の付いた Secret からしか読まない
        self.assertEqual(self.run_oidc().returncode, 0)
        made = self.made()
        self.assertEqual(made["argocd/argocd-oidc-keycloak"]["manifest"]["metadata"]["labels"], {"app.kubernetes.io/part-of": "argocd"})
        self.assertNotIn("labels", made["observability/grafana-oidc"]["manifest"]["metadata"])

    def test_existing_secrets_are_replaced(self):
        r = self.run_oidc(FAKE_EXISTING="argocd-oidc-keycloak grafana-oidc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({e["verb"] for e in self.entries() if e["manifest"].get("kind") == "Secret"}, {"replace"})

    def test_values_are_not_on_the_command_line(self):
        self.assertEqual(self.run_oidc().returncode, 0)
        for call in self.calls():
            self.assertNotIn("SECRET-", call)
            self.assertIn("--context kind-test", call)

    def test_missing_file_stops_before_any_secret(self):
        # ファイルを作るのは keycloak-secrets.sh だけ (realm の import と同じ値)。ここで作ると realm の側とずれる
        (self.dir / "grafana-prod").unlink()
        r = self.run_oidc()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("grafana-prod", r.stderr)
        self.assertEqual(self.made(), {})
        self.assertFalse((self.dir / "grafana-prod").exists())


class ArgoCD(unittest.TestCase):
    def setUp(self):
        self.values = (ROOT / "clusters/kind/argocd/values.yaml").read_text()

    def test_oidc_config_points_at_keycloak_not_dex(self):
        self.assertIn(f"      issuer: {ISSUER}\n", self.values)
        self.assertIn("      clientID: argocd\n", self.values)
        self.assertIn("      clientSecret: $argocd-oidc-keycloak:clientSecret\n", self.values)
        self.assertIn("      cliClientID: argocd-cli\n", self.values)
        self.assertRegex(self.values, r"\ndex:\n  enabled: false\n")
        self.assertNotIn("dex.config", self.values)

    def test_callback_url_is_a_redirect_uri_of_the_client(self):
        url = re.search(r"^    url: (\S+)$", self.values, re.M).group(1)
        self.assertIn(f"          - {url}/auth/callback\n", realm_client("argocd"))
        # argocd login --sso は localhost:8085 で待つ (argocd CLI の既定)
        self.assertIn("          - http://localhost:8085/auth/callback\n", realm_client("argocd-cli"))

    def test_groups_map_to_roles_and_nobody_else_gets_any(self):
        policy = re.search(r"policy\.csv: \|\n((?:      .*\n)+)", self.values).group(1)
        self.assertIn("      g, admins, role:admin\n", policy)
        self.assertIn("      g, viewers, role:readonly\n", policy)
        # policy.default を書くと、グループに入っていない人にもその権限が付く
        self.assertNotRegex(self.values, r"(?m)^    policy\.default:")

    def test_admin_password_login_is_kept(self):
        # 非常用。just/argocd-secrets.sh も admin で backstage のトークンを作る
        self.assertNotIn("admin.enabled", self.values)


class Grafana(unittest.TestCase):
    # values のファイル (後ろが勝つ) → clientId
    INSTANCES = {
        ("observability/grafana-values.yaml",): "grafana",
        ("env-grafana/values.yaml", "env-grafana/values-dev.yaml"): "grafana-dev",
        ("env-grafana/values.yaml", "env-grafana/values-prod.yaml"): "grafana-prod",
    }

    def setting(self, files, key):
        found = None
        for f in files:
            m = re.search(rf"^    {key}: (.+)$", (ROOT / "clusters/kind" / f).read_text(), re.M)
            found = m.group(1) if m else found
        return found

    def test_each_grafana_uses_its_own_client_and_callback(self):
        for files, client in self.INSTANCES.items():
            self.assertEqual(self.setting(files, "client_id"), client, files)
            root_url = self.setting(files, "root_url")
            self.assertIn(f"          - {root_url}/login/generic_oauth\n", realm_client(client), files)

    def test_endpoints_are_the_issuer_and_roles_come_from_groups(self):
        for files in self.INSTANCES:
            for key in ("auth_url", "token_url", "api_url"):
                self.assertTrue(self.setting(files, key).startswith(ISSUER + "/protocol/openid-connect/"), (files, key))
            self.assertEqual(self.setting(files, "role_attribute_path"),
                             "\"contains(groups[*], 'admins') && 'Admin' || contains(groups[*], 'viewers') && 'Viewer'\"")
            # グループに入っていない人はログインできない (既定の auto_assign_org_role の Viewer にしない)
            self.assertEqual(self.setting(files, "role_attribute_strict"), "true", files)

    def test_secret_comes_from_the_oidc_secret_and_password_login_stays(self):
        for files in self.INSTANCES:
            text = (ROOT / "clusters/kind" / files[0]).read_text()
            self.assertIn("  GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET:\n    secretKeyRef:\n      name: grafana-oidc\n      key: client-secret\n", text)
            self.assertNotIn("client_secret:", text, "secret は Git に置かない")
            # admin (非常用) と、share Pod・Backstage のプロキシの Basic 認証を残す
            self.assertNotIn("disable_login_form", text)
            self.assertNotIn("auth.basic", text)


if __name__ == "__main__":
    unittest.main()
