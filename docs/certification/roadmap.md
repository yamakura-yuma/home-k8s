# 学習ロードマップ

Kubestronaut / Golden Kubestronaut を見据えた資格取得の進捗チェックリスト。
演習・模擬問題は KodeKloud 等の外部プラットフォームを利用する前提。

プログラムの要件・特典・各試験の内容は [index.html](index.html) (ブラウザで開く) にまとめてある。

## Kubestronaut (必須5冠)

- [ ] KCNA (Kubernetes and Cloud Native Associate)
- [ ] KCSA (Kubernetes and Cloud Native Security Associate)
- [ ] CKA (Certified Kubernetes Administrator)
- [ ] CKAD (Certified Kubernetes Application Developer)
- [ ] CKS (Certified Kubernetes Security Specialist) ※CKA取得が前提条件

## Golden Kubestronaut (Kubestronaut達成後、CNCF全認定 + LFCS を追加取得)

選択式 (60問/90分):

- [ ] PCA (Prometheus Certified Associate)
- [ ] CCA (Cilium Certified Associate)
- [ ] CAPA (Certified Argo Project Associate)
- [ ] CGOA (Certified GitOps Associate)
- [ ] CBA (Certified Backstage Associate)
- [ ] OTCA (OpenTelemetry Certified Associate)
- [ ] KCA (Kyverno Certified Associate)
- [ ] CNPA (Certified Cloud Native Platform Engineering Associate) ※85問/120分

実技 (120分):

- [ ] ICA (Istio Certified Associate)
- [ ] CNPE (Certified Cloud Native Platform Engineer) ※2026-03-01 から要件
- [ ] LFCS (Linux Foundation Certified System Administrator) ※CNCF外だが必須

(2026-09 時点で16資格。新資格は GA から90日以上経過後に要件へ追加されるため、
CNCFの認定プログラム更新に応じて随時見直すこと。次は CKNE が予告済み)

## 将来課題

- kubeadmによる「本物」のマルチVM構成での構築練習
  - WSL2単体ではネスト仮想化が不安定なため未対応。
  - 候補: Multipass (Hyper-V) を使い Windows ホスト側でVMを立てる、
    または一時的にcrCloud VM (EC2等) を使う。
