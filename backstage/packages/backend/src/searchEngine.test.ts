import { LoggerService } from '@backstage/backend-plugin-api';
import { LunrSearchEngine } from '@backstage/plugin-search-backend-node';
import { EmptyOnMissingIndexLunrSearchEngine } from './searchEngine';

const logger: LoggerService = {
  child: () => logger,
  error: () => {},
  warn: () => {},
  info: () => {},
  debug: () => {},
};

async function index(engine: LunrSearchEngine, type: string) {
  const indexer = await engine.getIndexer(type);
  await new Promise<void>((resolve, reject) => {
    indexer.on('close', resolve);
    indexer.on('error', reject);
    indexer.end({ title: 'kind', text: 'kind cluster', location: '/docs/x' });
  });
}

describe('EmptyOnMissingIndexLunrSearchEngine', () => {
  it('上流の LunrSearchEngine は、索引の無い種類の検索で MissingIndexError を投げる', async () => {
    const engine = new LunrSearchEngine({ logger });
    await expect(
      engine.query({ term: 'kind', types: ['techdocs'] }),
    ).rejects.toThrow('Missing index for techdocs');
  });

  it('起動直後 (techdocs の索引が無い) の検索は 0 件を返す', async () => {
    const engine = new EmptyOnMissingIndexLunrSearchEngine({ logger });
    await expect(
      engine.query({ term: 'kind', types: ['techdocs'] }),
    ).resolves.toEqual({ results: [], numberOfResults: 0 });
  });

  it('索引ができた後は結果を返す', async () => {
    const engine = new EmptyOnMissingIndexLunrSearchEngine({ logger });
    await index(engine, 'techdocs');
    const result = await engine.query({ term: 'kind', types: ['techdocs'] });
    expect(result.numberOfResults).toBe(1);
    expect(result.results[0].document.location).toBe('/docs/x');
  });
});
