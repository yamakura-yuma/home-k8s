"""grafana.com の公開ダッシュボードを取ってきて、file provisioning で読める形にしてこのディレクトリに置く。

どれを入れるかは manifest.json (群 → フォルダと、名前 → ID・rev・元 URL)。JSON を手で直さず、
manifest の rev を上げてこのスクリプトを打ち直す:
    python3 clusters/kind/observability/dashboards/grafana-com/fetch.py
取得先は rev を固定した /revisions/<rev>/download (latest は使わない)。同じ rev なら何度打っても同じ
JSON になる。Grafana への反映は main に入れたあと ArgoCD が行う (群ごとに ConfigMap と provider grafana-com-<群>)。
ファイルを足したり消したりしたら dashboards/kustomization.yaml の files も直す。
"""
import json, os, re, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD = "https://grafana.com/api/dashboards/{id}/revisions/{rev}/download"

# __inputs のデータソース (pluginId) → grafana-values.yaml でプロビジョニングしている UID
DATASOURCE_UIDS = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}


def replace_strings(o, subs):
    """JSON のすべての文字列値の中の ${DS_...} を置き換える。"""
    if isinstance(o, dict):
        return {k: replace_strings(v, subs) for k, v in o.items()}
    if isinstance(o, list):
        return [replace_strings(v, subs) for v in o]
    if isinstance(o, str):
        return re.sub(r"\$\{(DS_[A-Za-z0-9_]+)\}", lambda m: subs.get(m.group(1), m.group(0)), o)
    return o


def normalize(d, entry):
    """grafana.com の書き出し形式 (UI のインポート画面向け) を file provisioning 向けにする。

    - __inputs / __requires / __elements を消す。__inputs の ${DS_...} は UI のインポート画面が
      置き換えるもので、file provisioning は解決しないので、ここでリテラルの UID に置き換える
    - id は null (Grafana が振る)。uid は上流のまま。上流が空なら manifest の uid を使う
      (Grafana は永続化していないので、空だと再起動のたびに uid が変わりリンクが切れる)
    - datasource 型の変数 (dotdc・15983 など) の選択を Prometheus にそろえる
    - description に元リンク (ID と rev) を足す
    """
    if d.get("__elements"):
        raise ValueError(f"{entry['id']}: __elements (ライブラリパネル) は扱えない")
    subs = {}
    for i in d.get("__inputs", []):
        if i.get("type") != "datasource":
            raise ValueError(f"{entry['id']}: datasource 以外の __inputs は扱えない: {i['name']}")
        subs[i["name"]] = DATASOURCE_UIDS[i["pluginId"]]
    d = {k: v for k, v in d.items() if k not in ("__inputs", "__requires", "__elements")}
    d = replace_strings(d, subs)
    d["id"] = None
    d["uid"] = d.get("uid") or entry["uid"]
    for v in d.get("templating", {}).get("list", []):
        if v.get("type") == "datasource" and v.get("query") == "prometheus":
            v["current"] = {"text": "Prometheus", "value": "prometheus"}
    source = f"Source: {entry['url']} (rev {entry['rev']})"
    d["description"] = f"{d['description']}\n\n{source}" if d.get("description") else source
    return d


def fetch(entry):
    req = urllib.request.Request(DOWNLOAD.format(**entry), headers={"User-Agent": "home-k8s-fetch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    manifest = json.load(open(os.path.join(HERE, "manifest.json")))
    n = 0
    for group, g in manifest.items():
        out = os.path.join(HERE, group)
        os.makedirs(out, exist_ok=True)
        # manifest から外したものは消す (消したら dashboards/kustomization.yaml からも外す)
        for f in os.listdir(out):
            if f.endswith(".json") and f[:-5] not in g["dashboards"]:
                os.remove(os.path.join(out, f))
        for name, entry in g["dashboards"].items():
            d = normalize(fetch(entry), entry)
            with open(os.path.join(out, name + ".json"), "w") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
                f.write("\n")
            n += 1
    print("wrote", n)


if __name__ == "__main__":
    sys.exit(main())
