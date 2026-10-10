"""Keycloak の Secret を作るスクリプト (keycloak-secrets.sh) と、CoreDNS の読み替え (coredns-tailnet.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_keycloak.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py) で、引数と標準入力だけを見る。稼働中のクラスタには触れない。
realm の宣言 (clusters/kind/auth/keycloak/realm-home-k8s.yaml) と Secret のキーの突き合わせも見る (YAML は読まず、行で見る)。
"""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_share_secrets import FAKE_RANDOM, FakeEnv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REALM = ROOT / "clusters/kind/auth/keycloak/realm-home-k8s.yaml"
CONFIDENTIAL = ["argocd", "grafana", "grafana-dev", "grafana-prod", "temporal-dev", "temporal-prod", "backstage", "headlamp", "oauth2-proxy"]
PUBLIC = ["argocd-cli", "kubernetes"]
HOST = "keycloak.taild2b611.ts.net"
TARGET = "keycloak-tailnet.auth.svc.cluster.local"

# kind (kubeadm) が作る Corefile (kind v0.32.0、CoreDNS v1.14.2 で確認)
COREFILE = """.:53 {
    errors
    health {
       lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf {
       max_concurrent 1000
    }
    cache 30 {
       disable success cluster.local
       disable denial cluster.local
    }
    loop
    reload
    loadbalance
}
"""


class KeycloakSecrets(FakeEnv):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "keycloak"

    def run_keycloak(self, **env):
        return self.run_script("keycloak-secrets.sh", str(self.dir), str(self.tmp), "kind-test", **env)

    def test_generates_files_once_and_makes_the_three_secrets(self):
        r = self.run_keycloak()
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(sorted(secrets), ["keycloak-bootstrap-admin", "keycloak-clients", "keycloak-postgres"])
        self.assertEqual(secrets["keycloak-postgres"][1], {"username": "keycloak", "password": FAKE_RANDOM})
        self.assertEqual(secrets["keycloak-bootstrap-admin"][1], {"username": "admin", "password": FAKE_RANDOM})
        self.assertEqual(sorted(secrets["keycloak-clients"][1]), sorted(CONFIDENTIAL))
        self.assertNotIn("last-applied", self.stdin_log.read_text())
        # 本人だけが読める
        self.assertEqual((self.dir / "postgres-password").stat().st_mode & 0o777, 0o600)

    def test_reuses_existing_files(self):
        # DB と realm はホストに残る。ファイルを作り直すと DB の側の値とずれるので、あれば読むだけ
        (self.dir / "clients").mkdir(parents=True)
        (self.dir / "postgres-password").write_text("KEEP-db")
        (self.dir / "clients" / "argocd").write_text("KEEP-argocd")
        r = self.run_keycloak(FAKE_EXISTING="keycloak-postgres keycloak-clients")
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(secrets["keycloak-postgres"], ("replace", {"username": "keycloak", "password": "KEEP-db"}))
        self.assertEqual(secrets["keycloak-clients"][1]["argocd"], "KEEP-argocd")
        self.assertEqual(secrets["keycloak-bootstrap-admin"][0], "create")

    def test_values_are_not_on_the_command_line(self):
        (self.dir / "clients").mkdir(parents=True)
        (self.dir / "admin-password").write_text("ADMIN-secret-value")
        r = self.run_keycloak()
        self.assertEqual(r.returncode, 0, r.stderr)
        for call in self.calls():
            self.assertNotIn("ADMIN-secret-value", call)
            self.assertNotIn(FAKE_RANDOM, call)

    def test_secret_keys_match_the_realm_placeholders(self):
        # realm の import は ${CLIENT_SECRET_*} を Secret keycloak-clients の clientId のキーから埋める。
        # キーが欠けると import の Job が失敗し、余れば使われない
        realm = REALM.read_text()
        keys = re.findall(r"secret: \{name: keycloak-clients, key: ([\w-]+)\}", realm)
        self.assertEqual(keys, CONFIDENTIAL)
        for key in keys:
            var = "CLIENT_SECRET_" + key.upper().replace("-", "_")
            self.assertIn(f"    {var}:\n", realm)
            self.assertIn(f"secret: ${{{var}}}\n", realm)


class Realm(unittest.TestCase):
    """realm home-k8s の宣言 (単位 3〜6 が前提にする値)。"""

    def setUp(self):
        self.realm = REALM.read_text()

    def test_all_clients_are_defined_once(self):
        ids = re.findall(r"^      - clientId: (\S+)$", self.realm, re.M)
        self.assertEqual(sorted(ids), sorted(CONFIDENTIAL + PUBLIC))

    def test_public_clients_use_pkce_and_have_no_secret(self):
        for block in re.split(r"\n      - clientId: ", self.realm)[1:]:
            cid = block.split("\n", 1)[0]
            if cid in PUBLIC:
                self.assertIn("publicClient: true", block, cid)
                self.assertIn("pkce.code.challenge.method: S256", block, cid)
                self.assertNotIn("secret:", block, cid)
            else:
                self.assertIn("publicClient: false", block, cid)

    def test_groups_claim_is_flat_names(self):
        # 各 UI の RBAC には admins・viewers と書く。full.path が true (Keycloak の既定) だと /admins になる
        self.assertIn("protocolMapper: oidc-group-membership-mapper", self.realm)
        self.assertIn('full.path: "false"', self.realm)
        self.assertIn("claim.name: groups", self.realm)
        self.assertRegex(self.realm, r"groups:\n      - name: admins\n      - name: viewers\n")
        # 既定の client scope (profile・email など) も作らせる
        self.assertIn('CreateDefaultClientScopes: "true"', self.realm)

    def test_redirects_point_at_the_tailnet(self):
        for uri in re.findall(r"^          - (https?://\S+)$", self.realm, re.M):
            self.assertTrue(uri.startswith("https://") and ".taild2b611.ts.net/" in uri or uri.startswith("http://localhost:"), uri)


class CorednsTailnet(FakeEnv):
    def setUp(self):
        super().setUp()
        self.corefile = self.tmp / "Corefile"
        self.corefile.write_text(COREFILE)

    def run_dns(self):
        return self.run_script("coredns-tailnet.sh", HOST, TARGET, "kind-test", FAKE_COREFILE=str(self.corefile))

    def patched(self):
        patches = [e["patch"] for e in self.entries() if e["verb"] == "patch-configmap"]
        self.assertEqual(len(patches), 1)
        head, body = patches[0].split("  Corefile: |\n", 1)
        self.assertEqual(head, "data:\n")
        return "".join(line[4:] + "\n" for line in body.splitlines())

    def test_inserts_the_rewrite_before_the_kubernetes_plugin(self):
        r = self.run_dns()
        self.assertEqual(r.returncode, 0, r.stderr)
        new = self.patched()
        self.assertIn(f"        name exact {HOST} {TARGET}\n        answer auto\n", new)
        self.assertLess(new.find("rewrite stop"), new.find("kubernetes cluster.local"))
        # 足した行を除けば元のまま
        kept = re.sub(r"    # home-k8s: begin.*?# home-k8s: end\n", "", new, flags=re.S)
        self.assertEqual(kept, COREFILE)

    def test_second_run_changes_nothing(self):
        self.run_dns()
        self.corefile.write_text(self.patched())
        self.stdin_log.unlink()
        r = self.run_dns()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.entries(), [])
        self.assertIn("読み替え済み", r.stdout)

    def test_replaces_an_older_rewrite(self):
        self.run_dns()
        self.corefile.write_text(self.patched().replace(TARGET, "old.auth.svc.cluster.local"))
        self.stdin_log.unlink()
        self.run_dns()
        new = self.patched()
        self.assertEqual(new.count("rewrite stop"), 1)
        self.assertNotIn("old.auth", new)

    def test_unknown_corefile_fails_without_patching(self):
        self.corefile.write_text(".:53 {\n    forward . /etc/resolv.conf\n}\n")
        r = self.run_dns()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("coredns-tailnet.sh", r.stderr)
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()
