"""Secret を作る処理すべて (#49) と、Grafana・ArgoCD の Secret を作るスクリプトの試験。ほかの試験 (test_oidc.py など) が使う偽の環境 (FakeEnv) もここに置く。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_secrets.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py)・curl・openssl で、引数と標準入力だけを見る。
稼働中のクラスタには触れない。
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

JUST = Path(__file__).resolve().parent
FAKE_KUBECTL = JUST / "fake_kubectl.py"
FAKE_RANDOM = "ab" * 32  # 偽の openssl rand -hex 32 の出力
SECRET_SCRIPTS = ["argocd-secrets.sh", "backstage-azure-secret.sh", "grafana-env-secrets.sh", "grafana-secrets.sh", "keycloak-secrets.sh", "tailscale-secrets.sh"]


def decode(manifest):
    return {key: base64.b64decode(value).decode() for key, value in manifest["data"].items()}


class FakeEnv(unittest.TestCase):
    """偽の kubectl・curl・openssl を PATH の先頭に置く。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.stdin_log = self.tmp / "stdin.log"
        self.write_stub("kubectl", f'exec python3 "{FAKE_KUBECTL}" "$@"')
        self.write_stub("curl", "echo 200")
        self.write_stub("openssl", f"echo {FAKE_RANDOM}")

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(0o755)

    def run_script(self, script, *args, **env):
        return subprocess.run(
            ["bash", str(JUST / script), *args], capture_output=True, text=True, cwd=self.tmp,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
                 "FAKE_KUBECTL_STDIN": str(self.stdin_log), "FAKE_EXISTING": "",
                 "HOME": str(self.tmp), **env})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def entries(self):
        return [json.loads(line) for line in self.stdin_log.read_text().splitlines()] if self.stdin_log.exists() else []

    def secrets(self):
        """標準入力で入った Secret を {名前: (動詞, 平文の data)} に。"""
        return {e["manifest"]["metadata"]["name"]: (e["verb"], decode(e["manifest"]))
                for e in self.entries() if e["manifest"].get("kind") == "Secret"}


class EverySecretCreatingScript(FakeEnv):
    """#49: Secret を作る処理すべてで、注釈に値が残らない。"""

    @staticmethod
    def statements(text):
        """行の継続 (末尾の \\) とパイプの続き (次の行の先頭の |) をつないだ、シェルの 1 文ずつ。"""
        return re.sub(r"\\\n|\n\s*(?=\|)", " ", text).splitlines()

    def test_scripts_do_not_pipe_secrets_into_apply(self):
        # 静的な見張り: Secret の manifest を kubectl apply に渡す文が無い。新しい Secret を足すときの取りこぼしを止める (#49)
        for name in SECRET_SCRIPTS:
            statements = self.statements((JUST / name).read_text())
            secret_statements = [s for s in statements if "create secret" in s]
            self.assertTrue(secret_statements, f"{name} に Secret を作る文が無い (試験が古い)")
            for statement in secret_statements:
                self.assertNotRegex(statement, r"\bapply\b", f"{name}: {statement}")

    def test_scripts_pass_only_non_secret_values_with_from_literal(self):
        # 静的な見張り: --from-literal は秘密でない値 (ユーザー名) だけ。秘密は base64 にしても値なので、
        # --from-file と一時ファイルで渡す (引数に出ると ps に残る。#56)。新しい鍵を足すときは、秘密でないと確かめてここに足す
        non_secret = {"admin-user", "username"}
        for name in SECRET_SCRIPTS:
            for statement in self.statements((JUST / name).read_text()):
                for key in re.findall(r"--from-literal=([^=\s]+)=", statement):
                    self.assertIn(key, non_secret, f"{name}: {statement}")

    def test_grafana_secrets_puts_all_four_without_annotation(self):
        files = {n: self.tmp / n for n in ("admin", "viewer", "backstage")}
        for name, path in files.items():
            path.write_text(f"{name.upper()}PW-secret")
        r = self.run_script("grafana-secrets.sh", *map(str, files.values()), "observability", str(JUST), "kind-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(sorted(secrets), ["backstage-grafana", "grafana-admin", "grafana-backstage", "grafana-viewer"])
        self.assertEqual(secrets["grafana-admin"][1], {"admin-user": "admin", "admin-password": "ADMINPW-secret"})
        self.assertEqual(secrets["backstage-grafana"][1]["GRAFANA_BASIC_AUTH"], base64.b64encode(b"backstage:BACKSTAGEPW-secret").decode())
        for verb, _ in secrets.values():
            self.assertIn(verb, ("create", "replace"))
        for e in self.entries():
            if e["manifest"].get("kind") == "Secret":
                self.assertNotIn("annotations", e["manifest"]["metadata"])
        self.assertEqual([c for c in self.calls() if "PW-secret" in c], [], "パスワードが kubectl の引数に出た")
        # Backstage の Basic は base64 でも値なので、引数に出ない (#56)
        basic = base64.b64encode(b"backstage:BACKSTAGEPW-secret").decode()
        self.assertEqual([c for c in self.calls() if basic in c or "from-literal" in c and "GRAFANA_BASIC_AUTH" in c], [],
                         "Backstage の Basic が kubectl の引数に出た")


class GrafanaEnvSecrets(FakeEnv):
    """環境 (dev・prod) ごとの Grafana の Secret (grafana-env-secrets.sh)。"""

    def run_env_secrets(self, *envs, **env):
        return self.run_script("grafana-env-secrets.sh", str(self.tmp / "pw"), str(JUST), "kind-test", *envs, **env)

    def test_puts_each_environments_secrets_and_one_backstage_secret(self):
        pw = self.tmp / "pw"
        pw.mkdir()
        (pw / "dev-admin-password").write_text("DEVADMIN-secret")
        (pw / "dev-backstage-password").write_text("DEVBS-secret")
        (pw / "prod-admin-password").write_text("PRODADMIN-secret")
        (pw / "prod-backstage-password").write_text("PRODBS-secret")
        r = self.run_env_secrets("dev", "prod")
        self.assertEqual(r.returncode, 0, r.stderr)
        puts = [(e["manifest"]["metadata"]["name"], e["verb"], decode(e["manifest"]))
                for e in self.entries() if e["manifest"].get("kind") == "Secret"]
        self.assertEqual([name for name, _, _ in puts],
                         ["grafana-admin", "grafana-backstage", "grafana-admin", "grafana-backstage", "backstage-grafana-env"])
        self.assertEqual(puts[0][2], {"admin-user": "admin", "admin-password": "DEVADMIN-secret"})
        self.assertEqual(puts[3][2], {"password": "PRODBS-secret"})
        # put_secret は Secret を丸ごと置き換えるので、Backstage 側は両方の環境のキーが 1 つの Secret に入る
        self.assertEqual(puts[4][2], {
            "GRAFANA_DEV_BASIC_AUTH": base64.b64encode(b"backstage:DEVBS-secret").decode(),
            "GRAFANA_PROD_BASIC_AUTH": base64.b64encode(b"backstage:PRODBS-secret").decode()})
        for _, verb, _ in puts:
            self.assertIn(verb, ("create", "replace"))
        for e in self.entries():
            if e["manifest"].get("kind") == "Secret":
                self.assertNotIn("annotations", e["manifest"]["metadata"])

    def test_generates_missing_passwords_once_and_reuses_them(self):
        r = self.run_env_secrets("dev")
        self.assertEqual(r.returncode, 0, r.stderr)
        files = sorted(p.name for p in (self.tmp / "pw").iterdir())
        self.assertEqual(files, ["dev-admin-password", "dev-backstage-password"])
        self.assertEqual((self.tmp / "pw" / "dev-admin-password").stat().st_mode & 0o077, 0, "本人以外が読める")
        self.assertEqual((self.tmp / "pw" / "dev-admin-password").read_text(), "ab" * 32)  # 偽の openssl の出力
        before = (self.tmp / "pw" / "dev-backstage-password").read_text()
        self.write_stub("openssl", "echo changed")
        self.assertEqual(self.run_env_secrets("dev").returncode, 0)
        self.assertEqual((self.tmp / "pw" / "dev-backstage-password").read_text(), before)

    def test_no_password_reaches_kubectl_arguments(self):
        pw = self.tmp / "pw"
        pw.mkdir()
        (pw / "dev-admin-password").write_text("DEVADMIN-secret")
        (pw / "dev-backstage-password").write_text("DEVBS-secret")
        basic = base64.b64encode(b"backstage:DEVBS-secret").decode()
        self.assertEqual(self.run_env_secrets("dev").returncode, 0)
        self.assertEqual([c for c in self.calls() if "-secret" in c or basic in c or "from-literal=GRAFANA" in c], [],
                         "パスワードか Basic が kubectl の引数に出た")

    def test_bad_environment_names_stop_before_touching_the_cluster(self):
        for bad in ("Dev", "dev prod", "../x", ""):
            r = self.run_env_secrets(bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual(self.calls(), [])


class ArgocdSecrets(FakeEnv):
    """argocd-secrets.sh: ArgoCD の読み取り専用アカウント backstage のトークンを、Git の外のファイルから Secret backstage-argocd にする。

    偽の kubectl は argocd-server の Pod の中で動かすスクリプトを手元の sh で動かし、PATH の先頭の偽の argocd を呼ぶ。
    """

    ADMIN = "admin-pw-0123456789"
    NEW = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJiYWNrc3RhZ2UifQ.sig-new-0123456789"
    OLD = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJiYWNrc3RhZ2UifQ.sig-old-0123456789"

    def setUp(self):
        super().setUp()
        self.token_file = self.tmp / "home-k8s" / "argocd" / "backstage-token"
        self.argocd_log = self.tmp / "argocd.log"
        self.execs = self.tmp / "execs"
        self.write_stub("sleep", "exit 0")
        # 偽の argocd: 呼ばれた副コマンドだけを記録する (パスワードやトークンはログに残さない)。
        # get-user-info は FAKE_VALID_TOKEN と同じ --auth-token のときだけ "Logged In: true"
        self.write_stub("argocd", """echo "$1 $2" >> "$ARGOCD_LOG"
case "$1" in
    login) ;;
    account)
        case "$2" in
            get-user-info)
                token=""
                while [ $# -gt 0 ]; do [ "$1" = "--auth-token" ] && token="$2"; shift; done
                if [ -n "$token" ] && [ "$token" = "$FAKE_VALID_TOKEN" ]; then echo "Logged In: true"; else echo "Logged In: false"; fi ;;
            generate-token) echo "$FAKE_NEW_TOKEN" ;;
        esac ;;
esac""")

    def run_argocd_secrets(self, **env):
        defaults = {"ARGOCD_LOG": str(self.argocd_log), "FAKE_ARGOCD_EXECS": str(self.execs),
                    "FAKE_ARGOCD_ADMIN_PASSWORD": self.ADMIN, "FAKE_NEW_TOKEN": self.NEW, "FAKE_VALID_TOKEN": ""}
        return self.run_script("argocd-secrets.sh", str(self.token_file), str(JUST), "kind-test", **{**defaults, **env})

    def argocd_calls(self):
        return self.argocd_log.read_text().splitlines() if self.argocd_log.exists() else []

    def test_creates_the_token_file_and_the_secret(self):
        r = self.run_argocd_secrets()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.token_file.read_text(), self.NEW)
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600, "トークンのファイルは本人だけが読める")
        secrets = self.secrets()
        self.assertEqual(list(secrets), ["backstage-argocd"])
        self.assertEqual(secrets["backstage-argocd"][1], {"ARGOCD_AUTH_TOKEN": self.NEW})
        self.assertIn(secrets["backstage-argocd"][0], ("create", "replace"))
        for e in self.entries():
            if e["manifest"].get("kind") == "Secret":
                self.assertEqual(e["manifest"]["metadata"]["namespace"], "backstage")
                self.assertNotIn("annotations", e["manifest"]["metadata"])
        # トークンのファイルが無いので、通るかの確認 (get-user-info) は挟まない
        self.assertEqual(self.argocd_calls(), ["login localhost:8080", "account generate-token"])
        # exec に渡すスクリプトは複数行で、ログでは行ごとに分かれる。kubectl の呼び出しの行だけを見る
        for call in [c for c in self.calls() if c.startswith("kubectl")]:
            self.assertIn("--context kind-test", call)

    def test_nothing_secret_reaches_the_command_line(self):
        self.assertEqual(self.run_argocd_secrets().returncode, 0)
        joined = "\n".join(self.calls())
        self.assertNotIn(self.ADMIN, joined, "admin のパスワードが kubectl の引数に出た")
        self.assertNotIn(self.NEW, joined, "トークンが kubectl の引数に出た")

    def test_a_token_that_argocd_still_accepts_is_reused(self):
        # 打ち直すたびに作ると、ArgoCD のアカウントにトークンが溜まる
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(self.OLD)
        r = self.run_argocd_secrets(FAKE_VALID_TOKEN=self.OLD)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("account generate-token", self.argocd_calls())
        self.assertEqual(self.token_file.read_text(), self.OLD)
        self.assertEqual(self.secrets()["backstage-argocd"][1], {"ARGOCD_AUTH_TOKEN": self.OLD})

    def test_a_token_that_argocd_rejects_is_replaced(self):
        # クラスタを作り直すと署名鍵が変わり、古いトークンは通らない
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(self.OLD)
        r = self.run_argocd_secrets(FAKE_VALID_TOKEN="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("account generate-token", self.argocd_calls())
        self.assertEqual(self.token_file.read_text(), self.NEW)
        self.assertEqual(self.secrets()["backstage-argocd"][1], {"ARGOCD_AUTH_TOKEN": self.NEW})

    def test_waits_for_argocd_to_load_the_account(self):
        # accounts.backstage を入れた直後は ArgoCD がまだ読んでいない (account does not exist)。少し待って打ち直す
        r = self.run_argocd_secrets(FAKE_ARGOCD_FAILURES="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.execs.read_text(), "4")
        self.assertEqual(self.token_file.read_text(), self.NEW)

    def test_gives_up_without_writing_anything(self):
        r = self.run_argocd_secrets(FAKE_ARGOCD_FAILURES="99")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.execs.read_text(), "10")
        self.assertFalse(self.token_file.exists())
        self.assertEqual(self.secrets(), {}, "トークンが無いのに Secret を作った")

    def test_output_that_is_not_a_token_is_refused(self):
        r = self.run_argocd_secrets(FAKE_NEW_TOKEN="FATA[0000] rpc error: boom")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.token_file.exists())
        self.assertEqual(self.secrets(), {})
        self.assertNotIn("boom", r.stderr + r.stdout, "トークンでない出力をそのまま表示しない")


if __name__ == "__main__":
    unittest.main()
