// home-k8s の Backstage のバックエンド。カタログ (Git の catalog-info.yaml)、
// Grafana プラグインが Grafana の API を読むためのプロキシ、TechDocs (文書の build と配信) だけを載せる。
import { createBackend } from '@backstage/backend-defaults';

const backend = createBackend();

backend.add(import('@backstage/plugin-app-backend'));
backend.add(import('@backstage/plugin-proxy-backend'));

// ログインはゲストだけ (127.0.0.1 と、パスワード付きの just share にしか出さない)
backend.add(import('@backstage/plugin-auth-backend'));
backend.add(import('@backstage/plugin-auth-backend-module-guest-provider'));

backend.add(import('@backstage/plugin-catalog-backend'));
backend.add(import('@backstage/plugin-catalog-backend-module-logs'));

// 文書は Pod の中で mkdocs が build する (app-config.yaml の techdocs)
backend.add(import('@backstage/plugin-techdocs-backend'));

backend.start();
