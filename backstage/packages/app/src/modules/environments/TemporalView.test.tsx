import { render, screen } from '@testing-library/react';
import { TemporalView, temporalEmbedUrl } from './TemporalView';

describe('temporalEmbedUrl', () => {
  it('http(s) の URL だけを返す', () => {
    expect(temporalEmbedUrl('http://localhost:8233')).toBe(
      'http://localhost:8233/',
    );
    expect(temporalEmbedUrl('https://temporal.example.com/')).toBe(
      'https://temporal.example.com/',
    );
    expect(temporalEmbedUrl('javascript:alert(1)')).toBeUndefined();
    expect(temporalEmbedUrl('not a url')).toBeUndefined();
    expect(temporalEmbedUrl(undefined)).toBeUndefined();
  });
});

describe('TemporalView', () => {
  it.each([
    ['dev', 'http://localhost:8233/'],
    ['prod', 'http://localhost:8234/'],
  ])('%s: その環境の Web UI を iframe で出す', (name, url) => {
    render(<TemporalView env={{ name, values: { 'temporal-url': url } }} />);
    const frame = screen.getByTitle(`Temporal UI (${name})`);
    expect(frame.getAttribute('src')).toBe(url);
  });

  it('URL が無い・http(s) でないときは iframe を出さず、案内を出す', () => {
    render(
      <TemporalView
        env={{ name: 'dev', values: { 'temporal-url': 'javascript:alert(1)' } }}
      />,
    );
    expect(screen.queryByTitle('Temporal UI (dev)')).toBeNull();
    expect(screen.getByText('Temporal UI の URL がありません')).toBeTruthy();
  });
});
