// Backstage のバックエンドの trace (OpenTelemetry)。node の --require で、バックエンドより先に読む
// (clusters/kind/backstage/values.yaml の NODE_OPTIONS。イメージでは /app/instrumentation.js)。
//
// 出すのは gitops-deployer の Scaffolder のアクション (packages/backend/src/gitopsDeployer.ts) の span と、その中の
// bff への呼び出し (fetch = undici) だけ。undici の計装は親の span があるときだけ span を作るので (requireParentforSpans)、
// カタログの GitHub の読み込みなど、ほかの外向きの呼び出しは trace にしない。受けた HTTP の要求も計装しない。
// traceparent は fetch のヘッダに入り、Gateway → APISIX → bff → … の trace に繋がる (docs/cluster/gitops-deployer-backstage.md)。
// OTEL_EXPORTER_OTLP_ENDPOINT が無ければ何もしない (手元の yarn start・試験)。
if (process.env.OTEL_EXPORTER_OTLP_ENDPOINT) {
  const { NodeTracerProvider, BatchSpanProcessor } = require('@opentelemetry/sdk-trace-node');
  const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  const { UndiciInstrumentation } = require('@opentelemetry/instrumentation-undici');
  const { resourceFromAttributes } = require('@opentelemetry/resources');

  const provider = new NodeTracerProvider({
    resource: resourceFromAttributes({ 'service.name': process.env.OTEL_SERVICE_NAME || 'backstage' }),
    spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter())],
  });
  provider.register();
  registerInstrumentations({
    instrumentations: [new UndiciInstrumentation({ requireParentforSpans: true })],
  });
  process.on('SIGTERM', () => provider.shutdown().catch(() => undefined));
}
