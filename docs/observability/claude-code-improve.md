# Claude Code のハーネスを直すためのダッシュボード

Grafana の `Claude Code improve` (`/d/claude-code-improve`) は、エージェントの使い方
(rule / skill / 指示の書き方 / モデル選択) のどこを直すかを決めるための画面。
データは Loki のイベント (依頼文・ツールの詳細・コスト) と Prometheus のトークン数から作る。
3 種類のデータと保存先は [claude-code-usage.md](claude-code-usage.md) にまとめている。

## 改善の回し方

1. **困りごとを 1 つ決める。** 例: 「依頼が高い」「同じコマンドで何度も失敗する」「入れた skill が使われない」。
2. **数字を見る。** 上段の「0. 比べる数字」から困りごとに合うものを 1 つ選び、今の値を控える。
   右上の期間 (例: Last 7 days) も控える。
3. **1 か所だけ変える。** rule を 1 行足す、skill の description を直す、spec の書き方を変える、
   モデルを変える、のどれか 1 つ。2 つ同時に変えると、どちらが効いたか分からない。
4. **同じ数字で比べる。** 変えてから同じ長さの期間が経ったら、同じパネル・同じ期間の長さで見比べる。
   依頼の中身で数字は大きく揺れるので、件数が 10 件未満のうちは判断しない。

## パネルの使い方

| 節 | パネル | 見方 | 疑うところ |
|---|---|---|---|
| 0 | 依頼数、1 依頼あたりのコスト・API 呼び出し・ツール呼び出し、ツールの失敗率、キャッシュ読み出し割合 | 変える前と後で比べる数字 | 各パネルの (i) |
| 1 | コストの高い依頼 | 依頼 (prompt.id) ごとのコスト上位 20。依頼文をクリックするとトレース、または同じ依頼の Loki のイベントへ | 短い依頼が上位 → 指示の書き方。Opus ばかり → モデル選択。往復が多い → rule / skill の手順不足 |
| 2 | 失敗したコマンド | success=false の tool_result をコマンド・パス・skill 名でまとめ、回数の多い順に並べる。右は直近の失敗 | 2 回以上並ぶもの → rule か skill に正しいコマンドを書く、hook で止める |
| 3 | skill / MCP の使われ方 | 名前ごとの呼び出し回数と、接続した MCP サーバーの呼び出し回数 | 0 回のもの → 外すか、description と使う場面を書き直す |
| 4 | 手戻りの目安 | ワーカーごとの依頼数と 1 依頼あたりの往復 | 依頼数が多い → spec で伝わっていない。往復が多い → そのリポジトリの CLAUDE.md / rule |
| 5 | キャッシュ読み出し | ワーカー別の割合と推移 (8 割が目安) | 低い → セッションの分けすぎ、途中のモデル切り替え、CLAUDE.md の頻繁な書き換え |
| 6 | 依頼文の検索 | 上の「依頼文」欄の語 (正規表現、大文字小文字は区別しない) を含む依頼を新しい順に | 似た依頼で安く済んだ書き方を rule / skill の例に取り込む |

上の「ワーカー」で Orca の worktree を絞れる。Orca の外で起動したセッションは `(Orca の外)` と出る。

### 数字の作り方と限界

- 依頼 = `user_prompt` イベント 1 件。コスト・API 呼び出し・ツール呼び出しは、同じ `prompt_id` を
  持つ `api_request` / `tool_result` を数える。表の各列は別々のクエリの結果を `prompt_id` で
  突き合わせている (Grafana の merge 変換)。
- 所要時間は、その依頼で最初と最後のイベントの時刻の差。人の返事を待った時間も含む。
- 依頼の「モデル」は、その依頼でいちばんコストがかかったモデル。
- 期間の端をまたぐ依頼は、期間内のイベントだけで数える。
- 依頼文・ツールの詳細が入るのは、Claude Code 側で `OTEL_LOG_USER_PROMPTS=1`・
  `OTEL_LOG_TOOL_DETAILS=1` を立てた後のイベントだけ。それより前の依頼文は `<REDACTED>`
  (Claude Code 自身の伏せ字) と出る。
- セッション単位ではなくワーカー単位でまとめている。イベントにセッション ID (`session_id`) が
  入るのは 2026-10-01 に `OTEL_METRICS_INCLUDE_SESSION_ID=true` にした後のイベントだけで、
  それより前のイベントには無いため。

## 秘密の値を伏せる

依頼文・Bash のコマンド・応答文には、うっかりトークンやパスワードが入ることがある。
OTel Collector の `transform/redact` processor (`clusters/kind/observability/otel-collector-values.yaml`)
で、Tempo と Loki に入れる前に `<REDACTED>` に置き換える。ログとスパンの属性の全部と、
ログ本文に同じ規則を当てる。メトリクスには文字列が入らないので当てない。

| 規則 | 例 | 伏せ方 |
|---|---|---|
| PEM の秘密鍵 | `-----BEGIN ... PRIVATE KEY-----` 〜 `END` (END が無ければ末尾まで) | ブロックごと |
| キーワードの後ろの値 | `Authorization: ...`、`Bearer ...`、`password=...`、`--password ...` | キーワードを残して値だけ |
| 形で分かるトークン | `sk-...` (`sk-ant-` を含む)、`ghp_` などの GitHub トークン、`github_pat_...`、`AKIA...` / `ASIA...`、`xoxb-` などの Slack トークン | トークンごと |

正規表現に合わない形の秘密 (ただの乱数文字列など) は伏せられない。依頼文やコマンドに秘密を
直接書かないのが先で、これは取りこぼしの保険。規則を足すときは、3 か所 (ログの属性、ログ本文、
スパンの属性) に同じものを足す。

確かめ方: 偽の値を含む依頼を流し、Loki で元の値が 0 件になることを見る。

```sh
echo "fake values for a redaction test. Run: echo 'Authorization: Bearer FAKEtoken1234567890 password=fakepw123'" \
  | OTEL_RESOURCE_ATTRIBUTES=orca.worktree.name=test-redact claude -p --allowedTools Bash
# 20 秒ほど待ってから
curl -s -u "admin:$(cat ~/.local/share/home-k8s/observability/grafana-admin-password)" -G \
  http://localhost:3000/api/datasources/proxy/uid/loki/loki/api/v1/query_range \
  --data-urlencode 'query={service_name="claude-code"} |~ "FAKEtoken|fakepw"' --data-urlencode since=10m \
  | jq '.data.result | length'   # 0 なら伏せられている
```

## ファイル

- ダッシュボード: `clusters/kind/observability/dashboards/claude-code-improve.json` (`just observe-up` が provisioning で入れる)
- 伏せ字: `clusters/kind/observability/otel-collector-values.yaml` の `transform/redact`
