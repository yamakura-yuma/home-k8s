// home-k8s の Backstage のバックエンド。カタログ (Git の catalog-info.yaml)、
// Grafana プラグインが Grafana の API を読むためのプロキシ、TechDocs (文書の build と配信) と検索だけを載せる。
import { createBackend } from '@backstage/backend-defaults';
import { azureResourcesFeatureLoader } from './azureResources';
import { azureSitesFeatureLoader } from './azureSites';
import { searchModuleEmptyOnMissingIndex } from './searchEngine';

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
// まだ索引の無い種類の検索 (起動直後の techdocs) を 500 でなく 0 件で返す (searchEngine.ts)
backend.add(searchModuleEmptyOnMissingIndex);
backend.add(import('@backstage/plugin-search-backend-module-catalog'));
backend.add(import('@backstage/plugin-search-backend-module-techdocs'));

// Azure のタブ (環境ごとの Azure のリソースを読む)。資格情報があるときだけ公式のプラグインを載せる (azureSites.ts)
backend.add(azureSitesFeatureLoader);

// Azure のリソースを Resource としてカタログに取り込む。資格情報があるときだけ載せる (azureResources.ts)
backend.add(azureResourcesFeatureLoader);

backend.start();
