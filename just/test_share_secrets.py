"""share Pod の Secret を作るスクリプト (share-secrets.sh) と、Secret を作る処理すべて (#49)、URL を引く関数 (share-urls.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_share_secrets.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py)・docker・curl・openssl で、引数と標準入力だけを見る。
稼働中のクラスタにも、ホストの docker にも触れない。
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
ENTRYPOINT = JUST.parent / "clusters/kind/share/entrypoint.sh"
VIEWER_VALUE = "viewer-pw-0123456789"
FAKE_RANDOM = "ab" * 32  # 偽の openssl rand -hex 32 の出力
SECRET_SCRIPTS = ["argocd-secrets.sh", "grafana-secrets.sh", "share-relay.sh", "share-secrets.sh"]


def decode(manifest):
    return {key: base64.b64decode(value).decode() for key, value in manifest["data"].items()}


class FakeEnv(unittest.TestCase):
    """偽の kubectl・docker・curl・openssl を PATH の先頭に置く。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.stdin_log = self.tmp / "stdin.log"
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.write_stub("kubectl", f'exec python3 "{FAKE_KUBECTL}" "$@"')
        self.write_stub("curl", "echo 200")
        self.write_stub("openssl", f"echo {FAKE_RANDOM}")
        self.viewer_file = self.tmp / "viewer-password"
        self.viewer_file.write_text(VIEWER_VALUE)

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(0o755)

    def run_script(self, script, *args, **env):
        return subprocess.run(
            ["bash", str(JUST / script), *args], capture_output=True, text=True, cwd=self.tmp,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
                 "FAKE_KUBECTL_STDIN": str(self.stdin_log), "FAKE_LOGS": str(self.logs), "FAKE_EXISTING": "",
                 "HOME": str(self.tmp), **env})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def entries(self):
        return [json.loads(line) for line in self.stdin_log.read_text().splitlines()] if self.stdin_log.exists() else []

    def secrets(self):
        """標準入力で入った Secret を {名前: (動詞, 平文の data)} に。"""
        return {e["manifest"]["metadata"]["name"]: (e["verb"], decode(e["manifest"]))
                for e in self.entries() if e["manifest"].get("kind") == "Secret"}


class ShareSecrets(FakeEnv):
    def run_share_secrets(self, **env):
        return self.run_script("share-secrets.sh", str(self.viewer_file), "kind-test", **env)

    def test_creates_the_three_secrets(self):
        r = self.run_share_secrets()
        self.assertEqual(r.returncode, 0, r.stderr)
        created = {e["manifest"]["metadata"]["name"]: e["manifest"] for e in self.entries()
                   if e["verb"] in ("create", "create-secret", "replace")}
        self.assertEqual(sorted(created), ["share-credentials", "share-grafana", "share-session-key"])
        self.assertEqual(created["share-credentials"].get("data", {}), {}, "資格情報は空で作る (拒否が既定)")
        self.assertEqual(decode(created["share-grafana"]), {"viewer-password": VIEWER_VALUE})
        self.assertEqual(list(decode(created["share-session-key"])), ["key"])
        self.assertEqual(decode(created["share-session-key"])["key"], FAKE_RANDOM)
        for call in self.calls():
            self.assertIn("--context kind-test", call)

    def test_never_uses_apply_for_secrets(self):
        # apply は data を last-applied-configuration の注釈に残す (#49)
        self.assertEqual(self.run_share_secrets().returncode, 0)
        self.assertEqual([c for c in self.calls() if " apply " in c and "secret" in c.lower()], [])
        for e in self.entries():
            if e["manifest"].get("kind") == "Secret":
                self.assertNotEqual(e["verb"], "apply")
                self.assertNotIn("annotations", e["manifest"]["metadata"])

    def test_existing_credentials_and_key_are_never_replaced(self):
        # just up の打ち直しで、人の資格情報と署名鍵 (cookie) を消さない。viewer の写しは、値が変わりうるので毎回置き換える
        r = self.run_share_secrets(FAKE_EXISTING="share-credentials share-session-key share-grafana")
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(list(secrets), ["share-grafana"])
        self.assertEqual(secrets["share-grafana"][0], "replace")
        self.assertEqual([c for c in self.calls() if "share-credentials" in c and "create secret" in c], [])
        self.assertEqual([c for c in self.calls() if "--force" in c], [], "replace --force は動いている Pod の下で消す")

    def test_nothing_secret_reaches_the_command_line(self):
        self.assertEqual(self.run_share_secrets().returncode, 0)
        joined = "\n".join(self.calls())
        self.assertNotIn(VIEWER_VALUE, joined)
        self.assertNotIn(FAKE_RANDOM, joined, "署名鍵が kubectl の引数に出た")

    def test_refuses_without_the_viewer_password(self):
        self.viewer_file.write_text("")
        r = self.run_share_secrets()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.entries(), [], "viewer のパスワードが無いのに Secret を作った")


class ShareSecretsRecreatePod(FakeEnv):
    """#64: Secret が揃う前に起動して閉じている share Pod だけを、Secret が揃ったあとに作り直す。開いている Pod (2 回目以降の just up) には触れない。"""

    def closed_caddy_log(self):
        """閉じて起動した caddy のログ: 実物の entrypoint.sh (偽の caddy で、環境変数が空) が書く行そのもの。文言のずれを見つける。"""
        self.write_stub("caddy", 'echo "{\\"msg\\":\\"started\\"}"')
        r = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True, check=True,
                           env={k: v for k, v in {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}.items()
                                if k not in ("GRAFANA_VIEWER_PASSWORD", "SHARE_RELAY_TOKEN", "SHARE_RELAY_ADDR")})
        return r.stderr + r.stdout

    def run_share_secrets(self, **env):
        return self.run_script("share-secrets.sh", str(self.viewer_file), "kind-test", **env)

    def deletes(self):
        return [c for c in self.calls() if " delete " in c]

    def test_a_pod_that_started_closed_is_recreated_once_the_secrets_exist(self):
        (self.logs / "caddy").write_text(self.closed_caddy_log())
        r = self.run_share_secrets(FAKE_EXISTING="share-host")  # share-host は just up の前の段 (_share-relay-up) が作る
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deletes(), ["kubectl --context kind-test -n share delete pod -l app=share --wait=false"])
        # 作り直すのは Secret を入れたあと (先に消すと、新しい Pod がまた閉じて起動する)
        last_put = max(i for i, c in enumerate(self.calls()) if " -f -" in c)
        self.assertGreater(self.calls().index(self.deletes()[0]), last_put)
        self.assertIn("作り直", r.stdout)

    def test_an_open_pod_is_left_alone(self):
        # 2 回目以降の just up: caddy は Secret を読んで開いて起動している。Pod を作り直すと Quick Tunnel の URL が変わる
        (self.logs / "caddy").write_text('{"level":"info","msg":"serving initial configuration"}\n')
        for existing in ("share-host", "share-host share-credentials share-session-key share-grafana"):
            r = self.run_share_secrets(FAKE_EXISTING=existing)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.deletes(), [], existing)
        # 同じ run を重ねても (打ち直し)、開いている Pod には触れない
        self.assertEqual(self.run_share_secrets(FAKE_EXISTING="share-host").returncode, 0)
        self.assertEqual(self.deletes(), [])

    def test_no_pod_yet_does_nothing(self):
        # ArgoCD の同期の前 (新しいクラスタ): Pod が無く、kubectl logs は失敗する。just up は止まらず、何も消さない
        r = self.run_share_secrets(FAKE_EXISTING="share-host")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deletes(), [])

    def test_nothing_is_recreated_before_share_host_exists(self):
        # share-host が無いまま作り直しても、新しい Pod は閉じたまま。Pod に触れない
        (self.logs / "caddy").write_text(self.closed_caddy_log())
        r = self.run_share_secrets()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deletes(), [])

    def test_a_failed_run_is_recovered_by_running_again(self):
        # 1 回目は Pod を消せずに落ちる (API の一時的な失敗など)。Secret は既にある。2 回目 (全部ある) でも、閉じた Pod なら作り直す
        (self.logs / "caddy").write_text(self.closed_caddy_log())
        for _ in range(2):
            r = self.run_share_secrets(FAKE_EXISTING="share-host share-credentials share-session-key share-grafana")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.deletes()), 2, "Secret が既にあっても、閉じた Pod は打ち直しで作り直す")

    def test_only_the_pod_is_deleted_never_the_deployment(self):
        (self.logs / "caddy").write_text(self.closed_caddy_log())
        self.assertEqual(self.run_share_secrets(FAKE_EXISTING="share-host").returncode, 0)
        joined = "\n".join(self.calls())
        for word in ("rollout", "delete deploy", "delete deployment", "scale", "patch deploy", "--force", "--grace-period"):
            self.assertNotIn(word, joined)

    def test_the_marker_is_what_entrypoint_writes(self):
        self.assertIn("全経路を 503 で拒否", (JUST / "secret-lib.sh").read_text())
        self.assertIn("全経路を 503 で拒否", self.closed_caddy_log())


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
        # 静的な見張り: --from-literal は秘密でない値 (ユーザー名・中継の宛先) だけ。秘密は base64 にしても値なので、
        # --from-file と一時ファイルで渡す (引数に出ると ps に残る。#56)。新しい鍵を足すときは、秘密でないと確かめてここに足す
        non_secret = {"admin-user", "SHARE_RELAY_ADDR"}
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


class ShareUrls(FakeEnv):
    def urls(self, **logs):
        for name, text in logs.items():
            (self.logs / f"cloudflared-{name}").write_text(text)
        r = subprocess.run(["bash", str(JUST / "share-urls.sh"), "kind-test"], capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
                                "FAKE_KUBECTL_STDIN": str(self.stdin_log), "FAKE_LOGS": str(self.logs)})
        return r

    BOX = ("2026-10-03T00:00:00Z INF Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):\n"
           "2026-10-03T00:00:00Z INF |  https://{host}.trycloudflare.com  |\n"
           "2026-10-03T00:00:01Z INF Registered tunnel connection connIndex=0\n")

    def test_prints_one_url_per_target_in_order(self):
        r = self.urls(grafana=self.BOX.format(host="aaa-bbb-ccc"), headroom=self.BOX.format(host="ddd-eee-fff"),
                      backstage=self.BOX.format(host="ggg-hhh-iii"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            "grafana https://aaa-bbb-ccc.trycloudflare.com",
            "headroom https://ddd-eee-fff.trycloudflare.com",
            "backstage https://ggg-hhh-iii.trycloudflare.com"])
        for call in self.calls():
            self.assertIn("logs deploy/share -c cloudflared-", call)
            self.assertIn("-n share", call)
            self.assertIn("--context kind-test", call)

    def test_api_host_in_an_error_is_not_a_url(self):
        failed = 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: lookup failed\n'
        r = self.urls(grafana=failed, headroom=failed + self.BOX.format(host="ddd-eee-fff"), backstage=failed)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ["grafana -", "headroom https://ddd-eee-fff.trycloudflare.com", "backstage -"])

    def test_missing_logs_give_a_dash_and_a_failure(self):
        r = self.urls()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ["grafana -", "headroom -", "backstage -"])

    def test_first_url_wins(self):
        r = self.urls(grafana=self.BOX.format(host="old-one-aaa") + self.BOX.format(host="new-one-bbb"))
        self.assertEqual(r.stdout.splitlines()[0], "grafana https://old-one-aaa.trycloudflare.com")

    def test_usage(self):
        r = subprocess.run(["bash", str(JUST / "share-urls.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
