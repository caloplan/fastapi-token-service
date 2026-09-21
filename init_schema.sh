#!/usr/bin/env bash
#
# init_schema.sh — 在 metastorage 中注册 fastapi-token-service 需要的两个 type。
#
# 用法（在 token 服务目录下执行）：
#   SERVICE_NAME=default \
#   SUPERUSER_TOKEN=<superuser_jwt> \
#   META_URL=http://localhost:9093 \
#   ./init_schema.sh
#
# 必填（留空会退出并提示）：
#   SERVICE_NAME      调用方 JWT 的 service_name（CaloPlan 默认 default）
#   SUPERUSER_TOKEN   superuser / GLOBAL scope 的 JWT（注册 type 是平台级操作）
# 可选：
#   META_URL          metastorage 地址，默认 http://localhost:9093
#   FORCE=1           已存在时先删除再重建（谨慎：会影响已有数据）
#
set -euo pipefail

META_URL="${META_URL:-http://localhost:9093}"
SERVICE_NAME="${SERVICE_NAME:-}"
SUPERUSER_TOKEN="${SUPERUSER_TOKEN:-}"
FORCE="${FORCE:-0}"

# ── 占位检查 ────────────────────────────────────────────────
if [ -z "$SERVICE_NAME" ] || [ "$SERVICE_NAME" = "TODO" ]; then
  echo "[!] 请先设置 SERVICE_NAME（调用方 JWT 的 service_name，如 default）" >&2
  exit 1
fi
if [ -z "$SUPERUSER_TOKEN" ] || [ "$SUPERUSER_TOKEN" = "TODO" ]; then
  echo "[!] 请先设置 SUPERUSER_TOKEN（superuser / GLOBAL scope 的 JWT）" >&2
  exit 1
fi

API="$META_URL/api/v1/types"
AUTH="Authorization: Bearer ${SUPERUSER_TOKEN}"
JSON="Content-Type: application/json"

register_type() {
  local type_name="$1"
  local description="$2"
  local fields="$3"

  echo "── 注册 ${type_name} (service_name=${SERVICE_NAME}) ──"

  # 已存在则按 FORCE 决定删不删
  if [ "$FORCE" = "1" ]; then
    echo "   FORCE=1：删除已存在的 ${type_name}（如有）"
    curl -sS -o /dev/null -w "" -X DELETE \
      "${API}/${type_name}?service_name=${SERVICE_NAME}" \
      -H "$AUTH" || true
  fi

  local body
  body=$(cat <<EOF
{
  "type_name": "${type_name}",
  "service_name": "${SERVICE_NAME}",
  "description": "${description}",
  "schema_json": { "fields": ${fields} }
}
EOF
)

  local code
  code=$(curl -sS -o /tmp/token_schema_resp.json -w "%{http_code}" -X POST \
    "$API" \
    -H "$AUTH" -H "$JSON" \
    -d "$body")

  if [ "$code" = "201" ] || [ "$code" = "200" ]; then
    echo "   [OK] ${type_name} 注册成功"
  elif [ "$code" = "409" ]; then
    echo "   [SKIP] ${type_name} 已存在（如需重建设 FORCE=1）"
  else
    echo "   [FAIL] HTTP ${code}:" >&2
    cat /tmp/token_schema_resp.json >&2
    echo "" >&2
    return 1
  fi
}

# ── token_usage：实际落库字段（today_*/month_* 读时聚合，不落库）──
register_type \
  "token_usage" \
  "CaloPlan LLM Token 使用记录（每用户单份，entity_key=user_id）" \
  '{
    "user_id":           { "type": "integer", "required": true },
    "input_tokens":      { "type": "integer" },
    "output_tokens":      { "type": "integer" },
    "total_tokens":      { "type": "integer" },
    "request_count":     { "type": "integer" },
    "daily_usage":       { "type": "dict" },
    "updated_at":         { "type": "string" }
  }'

# ── token_quota：每用户可选配额覆盖（不配用服务端全局默认）──
register_type \
  "token_quota" \
  "CaloPlan 每用户 Token 配额覆盖（可选）" \
  '{
    "user_id":            { "type": "integer", "required": true },
    "per_request_limit":  { "type": "integer" },
    "daily_limit":        { "type": "integer" },
    "total_limit":        { "type": "integer" },
    "updated_at":         { "type": "string" }
  }'

echo ""
echo "完成。可用 GET ${API}?service_name=${SERVICE_NAME} 验证。"
