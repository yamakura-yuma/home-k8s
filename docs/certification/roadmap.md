# 学習ロードマップ

Kubestronaut / Golden Kubestronaut を見据えた資格取得の進捗チェックリスト。
演習・模擬問題は KodeKloud 等の外部プラットフォームを利用する前提。

## Kubestronaut (必須5冠)

- [ ] KCNA (Kubernetes and Cloud Native Associate)
- [ ] KCSA (Kubernetes and Cloud Native Security Associate)
- [ ] CKA (Certified Kubernetes Administrator)
- [ ] CKAD (Certified Kubernetes Application Developer)
- [ ] CKS (Certified Kubernetes Security Specialist) ※CKA取得が前提条件

## Golden Kubestronaut (Kubestronaut達成後、CNCF系認定を追加取得)

- [ ] PCA (Prometheus Certified Associate)
- [ ] ICA (Istio Certified Associate)
- [ ] CCA (Cilium Certified Associate)
- [ ] CGOA (Certified GitOps Associate)
- [ ] KCA (Kyverno Certified Associate)

(必要な認定はCNCFの認定プログラム更新に応じて随時見直すこと)

## 将来課題

- kubeadmによる「本物」のマルチVM構成での構築練習
  - WSL2単体ではネスト仮想化が不安定なため未対応。
  - 候補: Multipass (Hyper-V) を使い Windows ホスト側でVMを立てる、
    または一時的にcrCloud VM (EC2等) を使う。
