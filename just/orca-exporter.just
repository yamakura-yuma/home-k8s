# Orca のオーケストレーション (Run / Task / ワーカー / メッセージ) を OTLP で観測スタックに送る
# exporter (tools/orca-exporter)。構成と見方は docs/observability/orca-orchestration.md。
# orca CLI はホスト (WSL) の relay 経由でしか動かないので、このレシピは開発用コンテナに転送しない
# 例外。shebang のレシピは set shell (just/dev-shell) を通らず、打った場所 (ホスト) で動く。

# install は systemd のユーザーユニットとして入れて起動する (入れ直しも同じ)。uninstall は止めて消す (送信済みの状態は残す)。
# status は稼働状態・直近のログ・送った件数を表示する。

# Orca の exporter を操作する (action: install | uninstall | status)
orca-exporter action:
    #!/usr/bin/env bash
    {{ if action =~ '^(install|uninstall|status)$' { "" } else { error("usage: just orca-exporter <install|uninstall|status>") } }}
    exec bash "{{justfile_directory()}}/tools/orca-exporter/orca-exporter.sh" {{action}}
