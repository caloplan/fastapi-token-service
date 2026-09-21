"""Token 服务请求 / 响应模型（snake_case，对 SDK 暴露的 wire 契约）。"""

from pydantic import BaseModel, Field


class TokenUsageSnapshot(BaseModel):
    """用户 Token 使用快照（累计 + 当日 + 当月）。"""

    user_id: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    # 当日（UTC 自然日）
    today_input_tokens: int = 0
    today_output_tokens: int = 0
    today_total_tokens: int = 0
    today_request_count: int = 0
    # 当月（由 daily_usage 聚合推导，不落库）
    month_total_tokens: int = 0
    updated_at: str | None = None


class TokenQuota(BaseModel):
    """当前用户生效配额（全局默认 + per-user 覆盖合并后）。"""

    per_request_limit: int
    daily_limit: int
    monthly_limit: int | None = None
    total_limit: int | None = None  # 生命周期累计总上限（None=不限）


class CheckRequest(BaseModel):
    """LLM 调用前检查请求。"""

    # 本次请求预估 Token 数；缺省时仅校验当日已用是否超当日上限（保守兜底）
    estimated_tokens: int | None = Field(None, ge=0)


class CheckResponse(BaseModel):
    """check 响应：allowed=false 时 reason 为
    per_request_exceeded / daily_limit_exceeded / total_limit_exceeded / meta_unavailable。"""

    allowed: bool
    reason: str | None = None
    usage: TokenUsageSnapshot
    quota: TokenQuota


class ConsumeRequest(BaseModel):
    """LLM 完成后记账请求：usage 以 Server 获得的真实值为准。

    不接受 total_tokens 由调用方指定——服务端按 input+output 计算，
    防 Client 伪造 usage。model / provider 仅作追踪记录。
    """

    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    model: str | None = None
    provider: str | None = None


class ConsumeResponse(BaseModel):
    """consume 响应：over_limit=true 表示本次已使当日用量超过上限（已发生，
    由下一次 check 拦截，直到窗口重置）。"""

    usage: TokenUsageSnapshot
    quota: TokenQuota
    over_limit: bool = False


class UsageResponse(BaseModel):
    """GET /usage 响应。"""

    usage: TokenUsageSnapshot


class QuotaResponse(BaseModel):
    """GET /quota 响应。"""

    quota: TokenQuota


class RemainingResponse(BaseModel):
    """GET /remaining 响应（供 UI 展示：今日已用 / 今日剩余）。"""

    per_request_limit: int
    daily_limit: int
    daily_used: int
    daily_remaining: int
