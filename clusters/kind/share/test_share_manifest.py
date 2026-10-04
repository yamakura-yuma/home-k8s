"""share Pod の manifest (clusters/kind/share) と ArgoCD の Application (apps/share.yaml) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/share -p test_share_manifest.py
`kustomize build` した結果を yq で JSON にして読む。クラスタにもネットワークにも出ない。kustomize・yq が無い環境では飛ばす
(`just ci` は devShells.ci に入れて必ず走らせる)。確かめるのは、公開の入口を足していないこと (Service・Ingress が無い)、
Caddyfile が読む環境変数と Secret・ポートの食い違い、cloudflared が caddy の 3 つのポートに 1 本ずつ向くこと。
#64: Pod の Secret の参照がすべて optional で (Secret が無くても CreateContainerConfigError で止まらない)、caddy の起動スクリプト
(entrypoint.sh) が、Secret の値が揃わないときに全拒否の Caddyfile.closed を選ぶこと (偽の caddy で実行する)。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
CADDYFILE = (HERE / "Caddyfile").read_text(encoding="utf-8")
CLOSED_CADDYFILE = (HERE / "Caddyfile.closed").read_text(encoding="utf-8")
ENTRYPOINT = HERE / "entrypoint.sh"
ENTRYPOINT_TEXT = ENTRYPOINT.read_text(encoding="utf-8")
AUTH_PY = (HERE / "share_auth.py").read_text(encoding="utf-8")
NEEDS = unittest.skipUnless(shutil.which("kustomize") and shutil.which("yq"), "kustomize か yq が無い")


def yq_json(*args):
    out = subprocess.run(["yq", "-o=json", "-I=0", *args], capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def render():
    built = subprocess.run(["kustomize", "build", str(HERE)], capture_output=True, text=True, check=True).stdout
    out = subprocess.run(["yq", "-o=json", "-I=0", "."], input=built, capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@NEEDS
class ShareManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.by_kind = {}
        for doc in cls.docs:
            cls.by_kind.setdefault(doc["kind"], []).append(doc)
        cls.deployment = cls.by_kind["Deployment"][0]
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        cls.containers = {c["name"]: c for c in cls.pod["containers"]}

    def test_only_the_expected_kinds(self):
        self.assertEqual(sorted(self.by_kind), ["ConfigMap", "Deployment", "Role", "RoleBinding", "ServiceAccount"])
        for doc in self.docs:
            self.assertEqual(doc["metadata"].get("namespace"), "share", doc["kind"])

    def test_nothing_opens_a_listener_to_the_cluster_or_the_world(self):
        # 公開の入口は Cloudflare への外向き接続だけ。Service (NodePort・LoadBalancer・ClusterIP も) と Ingress を作らない
        for kind in ("Service", "Ingress", "HTTPRoute", "Gateway"):
            self.assertNotIn(kind, self.by_kind)
        for container in self.pod["containers"]:
            self.assertNotIn("ports", container, f"{container['name']} が containerPort を宣言した")
        self.assertFalse(self.pod.get("hostNetwork"))

    def test_one_replica_recreate(self):
        # Pod の再起動で Quick Tunnel の URL が変わる。2 つ立てず、入れ替えのときも重ねない
        self.assertEqual(self.deployment["spec"]["replicas"], 1)
        self.assertEqual(self.deployment["spec"]["strategy"], {"type": "Recreate"})

    def test_five_containers_with_pinned_images(self):
        self.assertEqual(sorted(self.containers), ["auth", "caddy", "cloudflared-backstage", "cloudflared-grafana", "cloudflared-headroom"])
        for name, container in self.containers.items():
            image = container["image"]
            tag = image.rpartition(":")[2]
            self.assertIn(":", image, name)
            self.assertNotIn(tag, ("latest", ""), f"{name}: タグを固定する")
            self.assertRegex(tag, r"\d", f"{name}: タグにバージョンが無い")

    def test_cloudflared_tunnels_point_at_the_three_caddy_ports(self):
        expected = {"cloudflared-grafana": 8081, "cloudflared-headroom": 8082, "cloudflared-backstage": 8083}
        metrics = set()
        for name, port in expected.items():
            args = self.containers[name]["args"]
            self.assertEqual(args[0], "tunnel", name)
            self.assertIn("--no-autoupdate", args, name)
            self.assertEqual(args[args.index("--url") + 1], f"http://127.0.0.1:{port}", name)
            metrics.add(args[args.index("--metrics") + 1])  # 同じ Pod の中で待ち受けがぶつからない
        self.assertEqual(len(metrics), 3)
        # Caddyfile の既定の待ち受けと同じポートで、3 つとも 127.0.0.1
        for port in expected.values():
            self.assertRegex(CADDYFILE, rf"SHARE_\w+_PORT:{port}\}}")

    def test_caddyfile_variables_are_all_provided(self):
        # Caddyfile の {$VAR} のうち既定の無いもの (空だと起動しない) が、Pod の環境に入る。Secret share-host は envFrom、Basic は起動コマンドが組み立てる
        code = "\n".join(line for line in CADDYFILE.splitlines() if not line.lstrip().startswith("#"))
        required = set(re.findall(r"\{\$(\w+)\}", code))
        self.assertEqual(required, {"SHARE_GRAFANA_BASIC", "SHARE_RELAY_TOKEN", "SHARE_RELAY_ADDR"})
        caddy = self.containers["caddy"]
        self.assertEqual([e["secretRef"]["name"] for e in caddy["envFrom"]], ["share-host"])
        # Basic は起動スクリプトが組み立てる。Secret の値が空のまま Caddyfile (空だと起動しない) に渡さないよう、スクリプトが空を先に見分ける (#64)
        self.assertIn("SHARE_GRAFANA_BASIC", ENTRYPOINT_TEXT)
        viewer = next(e for e in caddy["env"] if e["name"] == "GRAFANA_VIEWER_PASSWORD")
        self.assertEqual(viewer["valueFrom"]["secretKeyRef"], {"name": "share-grafana", "key": "viewer-password", "optional": True})
        for var in sorted(required - {"SHARE_GRAFANA_BASIC"}) + ["GRAFANA_VIEWER_PASSWORD"]:
            self.assertIn(f'[ -n "${var}" ]', ENTRYPOINT_TEXT, f"{var} が空のとき Caddyfile.closed にならない (Caddyfile は起動しない)")
        # auth は Secret を持たない (API で share-credentials・share-session-key を読む)
        self.assertNotIn("envFrom", self.containers["auth"])
        for container in self.pod["containers"]:
            for env in container.get("env", []):
                if re.search(r"token|password|secret|key", env["name"], re.I):
                    self.assertNotIn("value", env, f"{env['name']} の値を manifest に書いた")

    def test_configmaps_carry_the_repo_files_and_are_mounted(self):
        configmaps = {cm["metadata"]["name"]: cm for cm in self.by_kind["ConfigMap"]}
        volumes = {v["name"]: v["configMap"]["name"] for v in self.pod["volumes"] if "configMap" in v}
        self.assertEqual(sorted(volumes), ["auth", "caddyfile"])
        # 名前にハッシュが付く (中身を変えると Pod が作り直される)。mount している名前が、作った ConfigMap と一致する
        for volume, name in volumes.items():
            self.assertIn(name, configmaps, volume)
            self.assertRegex(name, r"^share-(caddy|auth)-[a-z0-9]{6,}$")
        self.assertEqual(configmaps[volumes["caddyfile"]]["data"],
                         {"Caddyfile": CADDYFILE, "Caddyfile.closed": CLOSED_CADDYFILE, "entrypoint.sh": ENTRYPOINT_TEXT})
        self.assertEqual(configmaps[volumes["auth"]]["data"], {"share_auth.py": AUTH_PY})
        # caddy は起動スクリプトから始まる (ConfigMap のファイルは実行ビットが無いので sh で読む)。スクリプトが Caddyfile か Caddyfile.closed を選ぶ
        caddy_mounts = {m["name"]: m["mountPath"] for m in self.containers["caddy"]["volumeMounts"]}
        self.assertEqual(caddy_mounts["caddyfile"], "/etc/caddy")
        self.assertEqual(self.containers["caddy"]["command"], ["sh", "/etc/caddy/entrypoint.sh"])
        self.assertIn('exec caddy run --config "$config" --adapter caddyfile', ENTRYPOINT_TEXT)
        self.assertIn("/app/share_auth.py", self.containers["auth"]["command"])

    def test_every_secret_reference_is_optional(self):
        # #64: Secret は just up が作るので、ArgoCD の同期が先だと無い。optional でない参照が 1 つでもあると Pod が CreateContainerConfigError で止まる。
        # 足した参照も自動で見張る (volume・env・envFrom の全部)。auth は Secret を参照せず API で読む (Secret が無ければ全部 401)
        refs = []
        for container in self.pod["containers"]:
            for source in container.get("envFrom", []):
                refs.append((container["name"], source["secretRef"]))
            for env in container.get("env", []):
                if "secretKeyRef" in env.get("valueFrom", {}):
                    refs.append((container["name"], env["valueFrom"]["secretKeyRef"]))
        for volume in self.pod.get("volumes", []):
            if "secret" in volume:
                refs.append((volume["name"], volume["secret"]))
        self.assertEqual(sorted(ref["name"] for _, ref in refs), ["share-grafana", "share-host"])
        for where, ref in refs:
            self.assertIs(ref.get("optional"), True, f"{where}: Secret {ref['name']} の参照が optional でない")
        self.assertNotIn("auth", {where for where, _ in refs})
        for container in self.pod["containers"]:
            self.assertNotIn("projected", container)
        self.assertNotIn("initContainers", self.pod, "init コンテナは Secret を待たない (待つと Pod が起動しない)")

    def test_rbac_reads_exactly_the_two_secrets(self):
        (role,) = self.by_kind["Role"]
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["share-credentials", "share-session-key"], "verbs": ["get"]}])
        (binding,) = self.by_kind["RoleBinding"]
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "share", "namespace": "share"}])
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        self.assertEqual(self.pod["serviceAccountName"], "share")
        # 認証サービスが読む Secret の名前は share_auth.py と同じ
        self.assertIn('CREDENTIALS_SECRET = "share-credentials"', AUTH_PY)
        self.assertIn('SESSION_KEY_SECRET = "share-session-key"', AUTH_PY)

    def test_no_probe_restarts_the_pod_and_none_uses_loopback_from_outside(self):
        # kubelet の probe は Pod の IP から来るので、127.0.0.1 だけで待ち受ける caddy・auth に tcpSocket・httpGet は届かない。exec の readiness だけ。
        # liveness は付けない (probe の失敗で再起動すると Quick Tunnel の URL が変わる)。cloudflared には何も付けない
        for name, container in self.containers.items():
            self.assertNotIn("livenessProbe", container, name)
            self.assertNotIn("startupProbe", container, name)
            if name.startswith("cloudflared"):
                self.assertNotIn("readinessProbe", container, name)
            else:
                self.assertEqual(list(container["readinessProbe"]), ["exec", "periodSeconds"], name)

    def test_containers_are_hardened(self):
        for name, container in self.containers.items():
            sc = container["securityContext"]
            self.assertIs(sc["allowPrivilegeEscalation"], False, name)
            self.assertIs(sc["readOnlyRootFilesystem"], True, name)
            self.assertEqual(sc["capabilities"]["drop"], ["ALL"], name)
            self.assertEqual(sc["capabilities"].get("add", []), ["NET_BIND_SERVICE"] if name == "caddy" else [], name)
            self.assertIn("limits", container["resources"], name)
        self.assertIs(self.pod["securityContext"]["runAsNonRoot"], True)

    def test_every_writable_path_is_a_volume(self):
        mounts = {m["mountPath"] for m in self.containers["caddy"]["volumeMounts"]}
        self.assertIn("/tmp", mounts)
        env = {e["name"]: e["value"] for e in self.containers["caddy"]["env"] if "value" in e}
        self.assertTrue(env["XDG_CONFIG_HOME"].startswith("/tmp/") and env["XDG_DATA_HOME"].startswith("/tmp/"))


class ClosedCaddyfile(unittest.TestCase):
    """全拒否の Caddyfile.closed (実物の caddy での応答は test_share_caddy.py)。"""

    code = "\n".join(line for line in CLOSED_CADDYFILE.splitlines() if not line.lstrip().startswith("#"))

    def test_proxies_nothing_and_asks_nobody(self):
        for word in ("reverse_proxy", "forward_auth", "import", "handle", "route", "header_up"):
            self.assertNotIn(word, self.code)
        self.assertEqual(re.findall(r"respond (\S+)", self.code), ["503", "503", "503"])

    def test_listens_where_the_real_caddyfile_does(self):
        # 同じ 3 つのポート (cloudflared が向く先)・127.0.0.1 だけ。ずれると、Secret が揃った前後で cloudflared がつながらなくなる
        def listeners(text):
            return re.findall(r"^http://:\{\$(SHARE_\w+_PORT):(\d+)\} \{\n\tbind 127\.0\.0\.1$", text, re.M)

        self.assertEqual(len(listeners(CLOSED_CADDYFILE)), 3)
        self.assertEqual(listeners(CLOSED_CADDYFILE), listeners(CADDYFILE))


class Entrypoint(unittest.TestCase):
    """caddy の起動スクリプト (entrypoint.sh) を、偽の caddy で実行する。選んだ設定ファイルと、渡った Grafana の Basic を見る。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = self.tmp / "caddy"
        stub.write_text('#!/bin/sh\necho "args=$*"\necho "basic=${SHARE_GRAFANA_BASIC-unset}"\n')
        stub.chmod(0o755)

    def run_entrypoint(self, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("GRAFANA_VIEWER_PASSWORD", "SHARE_RELAY_TOKEN", "SHARE_RELAY_ADDR", "SHARE_GRAFANA_BASIC")}
        r = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True, check=True,
                           env={**base, "PATH": f"{self.tmp}:{os.environ['PATH']}", "SHARE_CADDY_DIR": "/conf", **env})
        out = dict(line.split("=", 1) for line in r.stdout.splitlines())
        return out["args"], out["basic"], r.stderr

    complete = {"GRAFANA_VIEWER_PASSWORD": "pw 'with' %s $x", "SHARE_RELAY_TOKEN": "tok", "SHARE_RELAY_ADDR": "172.18.0.1:8788"}

    def test_complete_secrets_start_the_real_caddyfile_with_the_viewer_key(self):
        args, basic, _ = self.run_entrypoint(**self.complete)
        self.assertEqual(args, "run --config /conf/Caddyfile --adapter caddyfile")
        self.assertEqual(base64.b64decode(basic).decode(), "viewer:pw 'with' %s $x")
        self.assertNotIn("\n", basic)

    def test_no_secret_at_all_starts_the_closed_caddyfile(self):
        # share-host も share-grafana も無い (optional の参照で環境変数が無い)
        args, basic, stderr = self.run_entrypoint()
        self.assertEqual(args, "run --config /conf/Caddyfile.closed --adapter caddyfile")
        self.assertEqual(basic, "unset", "Basic を組み立てて渡さない")
        self.assertIn("503", stderr)

    def test_any_missing_or_empty_value_starts_the_closed_caddyfile(self):
        for name in self.complete:
            for how in ("missing", "empty"):
                env = {k: v for k, v in self.complete.items() if k != name}
                if how == "empty":
                    env[name] = ""
                args, basic, _ = self.run_entrypoint(**env)
                self.assertEqual(args, "run --config /conf/Caddyfile.closed --adapter caddyfile", (name, how))
                # base64("viewer:") は空でない。パスワードが無いのに Basic を組み立てて Caddyfile に渡さない
                self.assertEqual(basic, "unset", (name, how))

    def test_default_config_dir_is_where_the_configmap_is_mounted(self):
        env = {k: v for k, v in os.environ.items() if k != "SHARE_CADDY_DIR"}
        r = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True, check=True, env={**env, "PATH": f"{self.tmp}:{os.environ['PATH']}"})
        self.assertIn("--config /etc/caddy/Caddyfile.closed", r.stdout)


@NEEDS
class Application(unittest.TestCase):
    def test_application_syncs_this_directory_into_share(self):
        (app,) = yq_json(".", str(ROOT / "clusters/kind/argocd/apps/share.yaml"))
        self.assertEqual(app["metadata"], {"name": "share", "namespace": "argocd"})
        self.assertEqual(app["spec"]["source"]["path"], "clusters/kind/share")
        self.assertEqual(app["spec"]["destination"]["namespace"], "share")
        self.assertEqual(app["spec"]["syncPolicy"]["automated"], {"prune": True, "selfHeal": True})
        self.assertIn("CreateNamespace=true", app["spec"]["syncPolicy"]["syncOptions"])
        self.assertTrue((ROOT / app["spec"]["source"]["path"] / "kustomization.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
