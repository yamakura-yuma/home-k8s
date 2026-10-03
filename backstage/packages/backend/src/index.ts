// home-k8s の Backstage のバックエンド。カタログ (Git の catalog-info.yaml)、
// Grafana プラグインが Grafana の API を読むためのプロキシ、TechDocs (文書の build と配信) と検索だけを載せる。
import { createBackend } from '@backstage/backend-defaults';

const backend = createBackend();

backend.add(import('@backstage/plugin-app-backend'));
backend.add(import('@backstage/plugin-proxy-backend'));

// ログインはゲストだけ (127.0.0.1 と、人ごとの資格情報つきの share Pod にしか出さない)
backend.add(import('@backstage/plugin-auth-backend'));
backend.add(import('@backstage/plugin-auth-backend-module-guest-provider'));

backend.add(import('@backstage/plugin-catalog-backend'));
backend.add(import('@backstage/plugin-catalog-backend-module-logs'));

// 文書は Pod の中で mkdocs が build する (app-config.yaml の techdocs)
backend.add(import('@backstage/plugin-techdocs-backend'));

// TechDocs の文書の画面が検索欄を出し、検索の API が無いと開けないので載せる。索引はメモリに持つ (Lunr)
backend.add(import('@backstage/plugin-search-backend'));
backend.add(import('@backstage/plugin-search-backend-module-catalog'));
backend.add(import('@backstage/plugin-search-backend-module-techdocs'));

backend.start();
