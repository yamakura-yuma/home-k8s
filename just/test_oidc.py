"""ArgoCD・Grafana ×3・Temporal UI ×2・Backstage・Headlamp・kind の API server の OIDC (Keycloak) の試験。
Secret を作るスクリプト (oidc-secrets.sh) と、各 UI の values・API server の設定・RBAC と realm の突き合わせ。

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
from test_share_secrets import FAKE_RANDOM, FakeEnv, decode  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REALM = (ROOT / "clusters/kind/auth/keycloak/realm-home-k8s.yaml").read_text()
ISSUER = "https://keycloak.taild2b611.ts.net/realms/home-k8s"
# <namespace>/<Secret> → (キー, clientId)
SECRETS = {
    "argocd/argocd-oidc-keycloak": ("clientSecret", "argocd"),
    "observability/grafana-oidc": ("client-secret", "grafana"),
    "dev/grafana-oidc": ("client-secret", "grafana-dev"),
    "prod/grafana-oidc": ("client-secret", "grafana-prod"),
    "dev/temporal-oidc": ("client-secret", "temporal-dev"),
    "prod/temporal-oidc": ("client-secret", "temporal-prod"),
    "backstage/backstage-oidc": ("AUTH_OIDC_CLIENT_SECRET", "backstage"),
    "headlamp/headlamp-oidc": ("OIDC_CLIENT_SECRET", "headlamp"),
}
# Backstage の session の署名鍵。client のファイルからではなく、無いときだけ乱数で作る
SESSION = "backstage/backstage-session"


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
        self.assertEqual(sorted(made), sorted([*SECRETS, SESSION]))
        for name, (key, client) in SECRETS.items():
            self.assertEqual(made[name]["verb"], "create")
            self.assertEqual(decode(made[name]["manifest"]), {key: f"SECRET-{client}"}, name)
            self.assertNotIn("annotations", made[name]["manifest"]["metadata"], "apply の注釈を残さない (#49)")

    def test_backstage_session_secret_is_random_and_made_only_once(self):
        self.assertEqual(self.run_oidc().returncode, 0)
        made = self.made()[SESSION]
        self.assertEqual(made["verb"], "create-secret")
        self.assertEqual(decode(made["manifest"]), {"AUTH_SESSION_SECRET": FAKE_RANDOM})
        # 打ち直しでは作り直さない (動いている Pod の鍵と変わらないように)
        self.stdin_log.unlink()
        self.assertEqual(self.run_oidc(FAKE_EXISTING="backstage-session").returncode, 0)
        self.assertNotIn(SESSION, self.made())

    def test_argocd_secret_has_the_label_argocd_reads(self):
        # ArgoCD は oidc.config の $<Secret>:<キー> を、ラベル app.kubernetes.io/part-of: argocd の付いた Secret からしか読まない
        self.assertEqual(self.run_oidc().returncode, 0)
        made = self.made()
        self.assertEqual(made["argocd/argocd-oidc-keycloak"]["manifest"]["metadata"]["labels"], {"app.kubernetes.io/part-of": "argocd"})
        self.assertNotIn("labels", made["observability/grafana-oidc"]["manifest"]["metadata"])

    def test_existing_secrets_are_replaced(self):
        r = self.run_oidc(FAKE_EXISTING="argocd-oidc-keycloak grafana-oidc temporal-oidc backstage-oidc headlamp-oidc backstage-session")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({e["verb"] for e in self.entries() if e["manifest"].get("kind") == "Secret"}, {"replace"})

    def test_values_are_not_on_the_command_line(self):
        self.assertEqual(self.run_oidc().returncode, 0)
        for call in self.calls():
            self.assertNotIn("SECRET-", call)
            self.assertNotIn(FAKE_RANDOM, call)
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


class Temporal(unittest.TestCase):
    TS = "taild2b611.ts.net"

    def values(self, env):
        return (ROOT / f"clusters/kind/temporal/values-{env}.yaml").read_text()

    def env_value(self, text, name):
        m = re.search(rf"    - name: {name}\n      value: (.+)\n", text)
        return m.group(1).strip('"') if m else None

    def test_web_logs_in_with_its_own_client_and_callback(self):
        for env in ("dev", "prod"):
            text = self.values(env)
            self.assertEqual(self.env_value(text, "TEMPORAL_AUTH_ENABLED"), "true", env)
            self.assertEqual(self.env_value(text, "TEMPORAL_AUTH_PROVIDER_URL"), ISSUER, env)
            self.assertEqual(self.env_value(text, "TEMPORAL_AUTH_CLIENT_ID"), f"temporal-{env}", env)
            callback = self.env_value(text, "TEMPORAL_AUTH_CALLBACK_URL")
            self.assertEqual(callback, f"https://temporal-{env}.{self.TS}/auth/sso/callback", env)
            self.assertIn(f"          - {callback}\n", realm_client(f"temporal-{env}"), env)
            self.assertIn("    - name: TEMPORAL_AUTH_CLIENT_SECRET\n      valueFrom:\n        secretKeyRef:\n"
                          "          name: temporal-oidc\n          key: client-secret\n", text, env)

    def test_tailnet_ingress_goes_through_the_embed_proxy(self):
        # callback のホスト (Ingress temporal-<環境>) は、iframe 用の proxy を通る (Backstage のタブと同じ URL)
        for env in ("dev", "prod"):
            text = (ROOT / f"clusters/kind/tailscale/ingress/temporal-{env}.yaml").read_text()
            self.assertIn("      name: temporal-ui-embed\n      port:\n        number: 80\n", text, env)
            self.assertIn(f"        - temporal-{env}\n", text, env)

    def test_server_checks_the_token_and_reads_permissions(self):
        text = (ROOT / "clusters/kind/temporal/values.yaml").read_text()
        self.assertIn(f"          - {ISSUER}/protocol/openid-connect/certs\n", text)
        self.assertIn("      permissionsClaimName: permissions\n", text)
        self.assertIn("      authorizer: default\n      claimMapper: default\n", text)
        # 認可の無い内部の frontend が無いと、worker と namespace のジョブがトークン無しで拒まれる
        self.assertIn("  internal-frontend:\n    enabled: true\n", text)

    def test_realm_maps_groups_to_temporal_permissions(self):
        for env in ("dev", "prod"):
            client = realm_client(f"temporal-{env}")
            self.assertIn(f"              usermodel.clientRoleMapping.clientId: temporal-{env}\n", client)
            self.assertIn("              claim.name: permissions\n", client)
            self.assertIn('              access.token.claim: "true"\n', client)
            self.assertIn(f"          temporal-{env}: [temporal-system:admin]\n", REALM.split("      - name: viewers")[0])
            self.assertIn(f"          temporal-{env}: [temporal-system:read]\n", REALM.split("      - name: viewers")[1])
            self.assertIn(f"        temporal-{env}:\n          - name: temporal-system:admin\n          - name: temporal-system:read\n", REALM)


class Backstage(unittest.TestCase):
    def setUp(self):
        self.config = (ROOT / "backstage/app-config.yaml").read_text()

    def test_oidc_provider_points_at_the_issuer_and_the_client(self):
        self.assertIn(f"        metadataUrl: {ISSUER}/.well-known/openid-configuration\n", self.config)
        self.assertIn("        clientId: backstage\n", self.config)
        self.assertIn("        clientSecret: ${AUTH_OIDC_CLIENT_SECRET}\n", self.config)
        self.assertIn("    secret: ${AUTH_SESSION_SECRET}\n", self.config)

    def test_callback_is_under_a_redirect_uri_of_the_client(self):
        # callback は backend.baseUrl/api/auth/oidc/handler/frame。realm の client は /api/auth/* を許す
        base = re.search(r"^backend:\n  baseUrl: (\S+)$", self.config, re.M).group(1)
        app = re.search(r"^app:\n  title: .*\n  baseUrl: (\S+)$", self.config, re.M).group(1)
        self.assertEqual(base, app)
        self.assertIn(f"          - {base}/api/auth/*\n", realm_client("backstage"))

    def test_secrets_reach_the_pod_and_permissions_are_on(self):
        values = (ROOT / "clusters/kind/backstage/values.yaml").read_text()
        self.assertIn("    - backstage-oidc\n    - backstage-session\n", values)
        self.assertIn("permission:\n  enabled: true\n", self.config)

    def test_tailnet_ingress_goes_through_the_guest_blocking_proxy(self):
        ingress = (ROOT / "clusters/kind/tailscale/ingress/backstage.yaml").read_text()
        self.assertIn("      name: backstage-tailnet\n      port:\n        number: 80\n", ingress)
        app = (ROOT / "clusters/kind/argocd/apps/backstage.yaml").read_text()
        self.assertIn("      path: clusters/kind/backstage/tailnet-proxy\n", app)


class Headlamp(unittest.TestCase):
    def setUp(self):
        self.values = (ROOT / "clusters/kind/headlamp/values.yaml").read_text()

    def env_value(self, name):
        m = re.search(rf"  - name: {name}\n    value: (.+)\n", self.values)
        return m.group(1).strip('"') if m else None

    def test_logs_in_with_the_headlamp_client_and_its_callback(self):
        self.assertEqual(self.env_value("OIDC_CLIENT_ID"), "headlamp")
        self.assertEqual(self.env_value("OIDC_ISSUER_URL"), ISSUER)
        self.assertEqual(self.env_value("OIDC_USE_PKCE"), "true")
        callback = self.env_value("OIDC_CALLBACK_URL")
        self.assertEqual(callback, "https://headlamp.taild2b611.ts.net/oidc-callback")
        self.assertIn(f"          - {callback}\n", realm_client("headlamp"))

    def test_secret_comes_from_the_secret_oidc_secrets_makes(self):
        # chart の externalSecret は envFrom。キーの名前がそのまま環境変数 (OIDC_CLIENT_SECRET) になる
        self.assertIn("    externalSecret:\n      enabled: true\n      name: headlamp-oidc\n", self.values)
        self.assertIn("    secret:\n      create: false\n", self.values)
        self.assertEqual(SECRETS["headlamp/headlamp-oidc"], ("OIDC_CLIENT_SECRET", "headlamp"))
        self.assertNotIn("clientSecret", self.values)


class KubeApiserver(unittest.TestCase):
    """kind の API server の AuthenticationConfiguration と、それを読ませる kind-config・RBAC・kubelogin の context。"""
    def setUp(self):
        self.authn = (ROOT / "clusters/kind/kube-apiserver/authentication-config.yaml").read_text()
        self.kind = (ROOT / "clusters/kind/kind-config.yaml").read_text()
        self.rbac = (ROOT / "clusters/kind/auth/rbac/oidc-groups.yaml").read_text()

    def test_issuer_and_audiences_are_the_realm_and_its_clients(self):
        self.assertIn(f"      url: {ISSUER}\n", self.authn)
        self.assertIn("      audiences:\n        - kubernetes\n        - headlamp\n      audienceMatchPolicy: MatchAny\n", self.authn)
        # ID トークンの aud は client の名前。どちらも realm にある
        realm_client("kubernetes")
        realm_client("headlamp")

    def test_claims_get_a_prefix(self):
        # 接頭辞が無いと、Keycloak のユーザー名で system: や ServiceAccount の名前を名乗れる
        self.assertIn('      username:\n        claim: preferred_username\n        prefix: "oidc:"\n', self.authn)
        self.assertIn('      groups:\n        claim: groups\n        prefix: "oidc:"\n', self.authn)

    def test_rbac_binds_the_prefixed_groups(self):
        docs = self.rbac.split("\n---\n")
        binds = {re.search(r"\n  name: (\S+)\n", d).group(1): (re.search(r"kind: ClusterRole\n  name: (\S+)", d).group(1),
                                                                  re.search(r"kind: Group\n    name: (\S+)", d).group(1))
                 for d in docs}
        self.assertEqual(binds, {
            "oidc-admins": ("cluster-admin", "oidc:admins"),
            "oidc-viewers": ("view", "oidc:viewers"),
            "oidc-viewers-cluster-read": ("headlamp-cluster-read", "oidc:viewers"),
        })
        # viewers に足す ClusterRole は読むだけ (Secret も無い)
        read = (ROOT / "clusters/kind/headlamp/manifests/rbac.yaml").read_text().split("name: headlamp-cluster-read\nrules:\n")[1].split("\n---\n")[0]
        self.assertEqual(set(re.findall(r"verbs: \[(.+)\]", read)), {"get, list, watch"})
        self.assertNotIn("secrets", read)

    def test_kind_config_mounts_the_directory_and_points_at_the_file(self):
        # just up (_kind-up) が clusters/kind/kube-apiserver をこのホストのディレクトリに置く
        self.assertIn("      - hostPath: ${HOME}/.local/share/home-k8s/kube-apiserver\n        containerPath: /etc/kubernetes/home-k8s\n", self.kind)
        self.assertIn("              value: /etc/kubernetes/home-k8s/authentication-config.yaml\n", self.kind)
        self.assertIn("          directory: /etc/kubernetes/home-k8s/patches\n", self.kind)
        self.assertTrue((ROOT / "clusters/kind/kube-apiserver/patches/kube-apiserver+strategic.yaml").exists())
        kind_just = (ROOT / "just/kind.just").read_text()
        self.assertIn('cp -r clusters/kind/kube-apiserver/. "$HOME/.local/share/home-k8s/kube-apiserver/"', kind_just)

    def test_apiserver_resolves_the_issuer_through_coredns(self):
        patch = (ROOT / "clusters/kind/kube-apiserver/patches/kube-apiserver+strategic.yaml").read_text()
        self.assertIn("spec:\n  dnsPolicy: ClusterFirstWithHostNet\n", patch)

    def test_kubelogin_context_uses_the_public_client_and_its_callbacks(self):
        recipe = (ROOT / "just/keycloak.just").read_text().split("_kube-oidc-context:\n")[1]
        self.assertIn("--exec-arg=--oidc-issuer-url=https://{{keycloak_host}}/realms/home-k8s", recipe)
        self.assertIn("--exec-arg=--oidc-client-id=kubernetes", recipe)
        self.assertIn("--exec-arg=--oidc-pkce-method=S256", recipe)
        self.assertNotIn("client-secret", recipe)
        # kubelogin は既定で 127.0.0.1:8000、だめなら 18000 で待つ。realm の redirect URI と同じ
        client = realm_client("kubernetes")
        self.assertIn("          - http://localhost:8000\n          - http://localhost:18000\n", client)
        self.assertIn("        publicClient: true\n", client)
        self.assertIn("          pkce.code.challenge.method: S256", client)


if __name__ == "__main__":
    unittest.main()
