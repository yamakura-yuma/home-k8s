// 検索の索引 (Lunr、メモリ) を、まだ索引の無い種類の検索で 500 を返さないものにする。
//
// Lunr は 0 件の種類の索引を作らない (LunrSearchEngine.getIndexer)。TechDocs の索引は build 済みの
// 文書の search_index.json から作るので、起動直後 (どの文書も build していない) は techdocs の索引が無い。
// そこへ TechDocs の文書画面の検索欄が types=techdocs で問い合わせると、LunrSearchEngine.query が
// MissingIndexError を投げ、search-backend の router がそのまま返して 500 になる。
// 索引が無いのは「まだ 1 件も無い」だけなので、0 件の結果として返す。
// 索引は今まで通り起動後と 10 分ごとに作り直し、文書を build した後の作り直しで結果が出るようになる。
import {
  coreServices,
  createBackendModule,
} from '@backstage/backend-plugin-api';
import {
  LunrSearchEngine,
  MissingIndexError,
} from '@backstage/plugin-search-backend-node';
import { searchEngineRegistryExtensionPoint } from '@backstage/plugin-search-backend-node/alpha';
import {
  IndexableResultSet,
  SearchQuery,
} from '@backstage/plugin-search-common';

export class EmptyOnMissingIndexLunrSearchEngine extends LunrSearchEngine {
  async query(query: SearchQuery): Promise<IndexableResultSet> {
    try {
      return await super.query(query);
    } catch (error) {
      if (error instanceof MissingIndexError) {
        return { results: [], numberOfResults: 0 };
      }
      throw error;
    }
  }
}

export const searchModuleEmptyOnMissingIndex = createBackendModule({
  pluginId: 'search',
  moduleId: 'empty-on-missing-index',
  register(env) {
    env.registerInit({
      deps: {
        searchEngineRegistry: searchEngineRegistryExtensionPoint,
        logger: coreServices.logger,
      },
      async init({ searchEngineRegistry, logger }) {
        searchEngineRegistry.setSearchEngine(
          new EmptyOnMissingIndexLunrSearchEngine({ logger }),
        );
      },
    });
  },
});
