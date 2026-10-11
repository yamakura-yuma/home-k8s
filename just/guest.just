# ゲスト (他の人) のアカウント。Keycloak の realm home-k8s のグループ guests に入れ、発行した日の次の 3:00 JST に消える
# (CronJob guest-expiry、clusters/kind/auth/guest-expiry)。本体は just/guest.sh、設計は docs/cluster/guest-access.md。
# 一時パスワードは add の最後に 1 回だけ表示する (引数・ファイル・ログに残さない)。ゲストに見せるのは Grafana (観測スタック) と Backstage だけ。

# ゲストを発行・一覧・取り消しする (just guest add <名前> | just guest list | just guest delete <名前>)
[positional-arguments]
guest action *args:
    {{ if action =~ '^(add|list|delete)$' { "" } else { error("usage: just guest <add <名前> | list | delete <名前>>") } }}
    @bash just/guest.sh {{kube_context}} "$@"
