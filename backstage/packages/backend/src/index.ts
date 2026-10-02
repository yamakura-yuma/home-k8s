// home-k8s の Backstage のバックエンド。カタログ (Git の catalog-info.yaml) と、
// Grafana プラグインが Grafana の API を読むためのプロキシだけを載せる。
import { createBackend } from '@backstage/backend-defaults';

const backend = createBackend();

backend.add(import('@backstage/plugin-app-backend'));
backend.add(import('@backstage/plugin-proxy-backend'));

// ログインはゲストだけ (127.0.0.1 にしか出さない)
backend.add(import('@backstage/plugin-auth-backend'));
backend.add(import('@backstage/plugin-auth-backend-module-guest-provider'));

backend.add(import('@backstage/plugin-catalog-backend'));
backend.add(import('@backstage/plugin-catalog-backend-module-logs'));

backend.start();
