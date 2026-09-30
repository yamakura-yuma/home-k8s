# Orca のオーケストレーション (Run / Task / ワーカー / メッセージ) を OTLP で観測スタックに送る
# exporter (tools/orca-exporter)。構成と見方は docs/observability/orca-orchestration.md。
# orca CLI はホスト (WSL) の relay 経由でしか動かないので、この 3 つは開発用コンテナに転送しない
# 例外。shebang のレシピは set shell (just/dev-shell) を通らず、打った場所 (ホスト) で動く。

# exporter を systemd のユーザーユニットとして入れて起動する (入れ直しも同じ)
orca-exporter-install:
    #!/usr/bin/env bash
    exec bash "{{justfile_directory()}}/tools/orca-exporter/orca-exporter.sh" install

# exporter を止めて消す (送信済みの状態は残す)
orca-exporter-uninstall:
    #!/usr/bin/env bash
    exec bash "{{justfile_directory()}}/tools/orca-exporter/orca-exporter.sh" uninstall

# exporter の稼働状態・直近のログ・送った件数を表示する
orca-exporter-status:
    #!/usr/bin/env bash
    exec bash "{{justfile_directory()}}/tools/orca-exporter/orca-exporter.sh" status
