import { proxyServerUrl, withServer } from './openapi';

const definition = `
openapi: 3.0.3
info:
  title: sample-api
  version: 0.1.0
servers:
  - url: /api/proxy/sample-api-dev
  - url: /api/proxy/sample-api-prod
paths:
  /info:
    get:
      responses:
        "200":
          description: OK
`;

describe('withServer', () => {
  it('servers を環境のプロキシ 1 つに差し替え、ほかは変えない', () => {
    const out = JSON.parse(
      withServer(definition, {
        url: 'http://localhost:7007/api/proxy/sample-api-prod',
        description: 'prod',
      }),
    );
    expect(out.servers).toEqual([
      {
        url: 'http://localhost:7007/api/proxy/sample-api-prod',
        description: 'prod',
      },
    ]);
    expect(out.openapi).toBe('3.0.3');
    expect(out.info.title).toBe('sample-api');
    expect(Object.keys(out.paths)).toEqual(['/info']);
  });

  it('servers が無い定義にも足す', () => {
    const out = JSON.parse(
      withServer('openapi: 3.0.3\npaths: {}\n', {
        url: '/p',
        description: 'x',
      }),
    );
    expect(out.servers).toEqual([{ url: '/p', description: 'x' }]);
  });

  it('オブジェクトでない定義は落とす', () => {
    expect(() =>
      withServer('- a\n- b\n', { url: '/p', description: 'x' }),
    ).toThrow();
    expect(() =>
      withServer('plain', { url: '/p', description: 'x' }),
    ).toThrow();
  });
});

describe('proxyServerUrl', () => {
  it('プロキシの基準 URL に環境の経路を続ける', () => {
    expect(
      proxyServerUrl('http://localhost:7007/api/proxy', '/sample-api-dev'),
    ).toBe('http://localhost:7007/api/proxy/sample-api-dev');
    expect(
      proxyServerUrl('http://localhost:7007/api/proxy/', '/sample-api-dev'),
    ).toBe('http://localhost:7007/api/proxy/sample-api-dev');
  });
});
