package bff

import _ "embed"

// OpenAPI は bff の API 契約 (Backstage の Scaffolder・タブが合わせる形)。GET /api/v1/openapi.yaml で返す。
//
//go:embed openapi.yaml
var OpenAPI []byte
