// In-memory fixtures, loaded only by `npm run demo`. No production connections.
const now = Date.now() / 1000;
let items = Array.from({ length: 2046 }, (_, index) => ({
  filename: `antigravity-account-${String(index + 1).padStart(4, "0")}.json`,
  user_email: `account.${String(index + 1).padStart(4, "0")}@example.com`,
  tier: index % 7 === 1 ? "ultra_lite" : index % 5 === 0 ? "ultra" : "pro",
  disabled: index % 31 === 9,
  error_codes: index % 19 === 7 ? [429] : index % 31 === 9 ? [403] : [],
  enable_credit: index % 9 === 2,
  last_success: now - index * 114,
  model_cooldowns: index % 13 === 4 ? { "gemini-3.8-flash": now + 2300 } : {},
}));
let settings = {
  credentials_dir: "./creds",
  proxy: "",
  host: "0.0.0.0",
  port: 7861,
  api_password: "preview-only",
  panel_password: "preview-only",
  oauth_proxy_url: "https://oauth2.googleapis.com",
  googleapis_proxy_url: "https://www.googleapis.com",
  antigravity_api_url: "https://daily-cloudcode-pa.googleapis.com",
  credential_retry_limit_enabled: true,
  max_retry_credentials: 15,
  credential_retry_interval: 0.1,
  session_affinity_enabled: true,
  session_affinity_ttl_seconds: 3600,
  return_thoughts_to_frontend: true,
  antigravity_stream2nostream: true,
  empty_output_error_enabled: true,
  compatibility_mode_enabled: false,
  keepalive_url: "",
  keepalive_interval: 300,
};
const windows = {
  gemini: {
    "5h": { remaining_percent: 76.4 },
    "7d": { remaining_percent: 63.8 },
  },
  claude: {
    "5h": { remaining_percent: 82.1 },
    "7d": { remaining_percent: 54.6 },
  },
};
const summary = {
  success: true,
  updated_at: now - 360,
  enabled_accounts: 1980,
  checked_accounts: 1980,
  model_summaries: Object.entries(windows).flatMap(([family, periods]) =>
    Object.entries(periods).map(([period, data]) => ({
      family,
      period,
      ...data,
    })),
  ),
};
let capture = { active: false, captured_count: 0, target_count: 3 };

export async function respond(config) {
  await new Promise((resolve) => setTimeout(resolve, 140));
  const url = new URL(config.url, "http://preview.local");
  const path = decodeURIComponent(url.pathname);
  const params = { ...Object.fromEntries(url.searchParams), ...config.params };
  const body =
    typeof config.data === "string"
      ? JSON.parse(config.data)
      : config.data || {};
  const result = (data) => ({
    data,
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  const fail = (message) => {
    const error = new Error(message);
    error.response = { status: 400, data: { detail: message } };
    throw error;
  };
  if (path === "/version/info")
    return result({
      success: true,
      version: "local-preview",
      antigravity_cli_version: "1.5.2",
      date: "本地开发",
      message: "上游 React 面板适配与样式优化",
      check_update: !!params.check_update,
      has_update: false,
    });
  if (path === "/auth/login") return result({ token: "preview-only" });
  if (path.startsWith("/auth/")) return fail("本地预览不发起真实 OAuth 授权。");
  if (path === "/config/get")
    return result({ config: { ...settings }, env_locked: ["host", "port"] });
  if (path === "/config/save") {
    settings = { ...settings, ...body.config };
    return result({ message: "已保存预览配置", saved_config: body.config });
  }
  if (path === "/config/request-capture/status")
    return result({ status: capture });
  if (path === "/config/request-capture/start") {
    capture = { ...capture, active: true };
    return result({ status: capture });
  }
  if (path === "/config/request-capture/stop") {
    capture = { ...capture, active: false };
    return result({ status: capture });
  }
  if (path === "/creds/status") {
    const abnormal = (item) => item.disabled || item.error_codes.length > 0;
    const filtered = items.filter(
      (item) =>
        (params.status_filter === "normal"
          ? !abnormal(item)
          : ["abnormal", "claude_abnormal", "gemini_abnormal"].includes(
                params.status_filter,
              )
            ? abnormal(item)
            : true) &&
        (!params.tier_filter ||
          params.tier_filter === "all" ||
          item.tier === params.tier_filter) &&
        (params.error_code_filter === "all" ||
          !params.error_code_filter ||
          item.error_codes.includes(Number(params.error_code_filter))) &&
        (params.cooldown_filter === "in_cooldown"
          ? Object.keys(item.model_cooldowns).length
          : params.cooldown_filter === "no_cooldown"
            ? !Object.keys(item.model_cooldowns).length
            : true),
    );
    const offset = Number(params.offset || 0),
      limit = Number(params.limit || 20);
    return result({
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      stats: {
        total: items.length,
        normal: items.filter((item) => !abnormal(item)).length,
        abnormal: items.filter(abnormal).length,
      },
    });
  }
  if (path.startsWith("/creds/antigravity-credit-summary"))
    return result({ running: false, result: summary });
  if (path === "/creds/action" || path === "/creds/batch-action") {
    const names = body.filenames || [body.filename];
    items = items
      .filter(
        (item) => !(names.includes(item.filename) && body.action === "delete"),
      )
      .map((item) =>
        !names.includes(item.filename)
          ? item
          : {
              ...item,
              ...(body.action === "enable"
                ? { disabled: false }
                : body.action === "disable"
                  ? { disabled: true }
                  : { enable_credit: body.action === "enable_credit" }),
            },
      );
    return result({ message: `已更新 ${names.length} 条示例凭证`, errors: [] });
  }
  if (path.startsWith("/creds/quota/"))
    return result({ success: true, supported: true, windows });
  if (path.startsWith("/creds/errors/"))
    return result({
      error_codes: [429],
      error_messages: { 429: "示例：上游暂时限流，稍后重试。" },
    });
  if (path.startsWith("/creds/detail/"))
    return result({
      filename: path.split("/").pop(),
      content: { note: "仅显示预览数据，不含真实凭据" },
    });
  if (
    path.startsWith("/creds/test/") ||
    path.startsWith("/creds/verify-project/")
  )
    return result({
      success: true,
      model: "gemini-3.5-flash-lite",
      message: "预览成功；未发送真实模型请求。",
      project_id: "preview-project",
      subscription_tier: "ultra_lite",
    });
  if (path === "/creds/upload")
    return fail("本地预览不导入真实凭据，请勿上传账号文件。");
  if (path === "/marked-prompts")
    return result({
      total: 3,
      items: Array.from({ length: 3 }, (_, index) => ({
        id: String(index + 1).repeat(64),
        model: "gemini-3.8-flash",
        created_at: now - index * 900,
        chars: 1680 + index * 320,
        preview: [
          "你是一位专业的编程助手，请清晰地解释解决方案…",
          "请按照指定格式整理文档，并保留重要信息…",
          "阅读下面的代码，找出可读性与性能方面的改进…",
        ][index],
      })),
    });
  if (config.responseType === "blob")
    return fail("此处仅预览界面，没有真实文件可下载。");
  return fail("该操作未在本地预览中启用。");
}
