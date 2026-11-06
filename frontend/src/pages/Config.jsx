import { useEffect, useState } from "react";
import { Check, Eye, EyeOff, Loader2, LockKeyhole, Save } from "lucide-react";
import api, { errorText } from "../api";

// These are the settings supported by this fork, not the removed upstream policies.
const groups = [
  {
    title: "请求与响应",
    note: "凭证切换、会话复用和响应行为。",
    fields: [
      [
        "credential_retry_limit_enabled",
        "限制凭证尝试次数",
        "bool",
        "单次请求限制尝试的凭证数量。",
      ],
      [
        "max_retry_credentials",
        "最多尝试凭证数",
        "number",
        "一次请求允许切换的凭证数量。",
        1,
      ],
      [
        "credential_retry_interval",
        "切换间隔（秒）",
        "number",
        "切换到下一个凭证前的等待时间。",
        0.01,
        10,
        0.01,
      ],
      [
        "session_affinity_enabled",
        "会话复用",
        "bool",
        "同一会话优先使用同一凭证。",
      ],
      [
        "session_affinity_ttl_seconds",
        "会话有效期（秒）",
        "number",
        "会话与凭证绑定的保留时间。",
        60,
        604800,
      ],
      [
        "return_thoughts_to_frontend",
        "返回思维内容",
        "bool",
        "在支持的响应中返回模型思维内容。",
      ],
      [
        "antigravity_stream2nostream",
        "流式转换为非流式",
        "bool",
        "在服务内部收集流式结果，用于非流式请求。",
      ],
      [
        "empty_output_error_enabled",
        "空响应报错",
        "bool",
        "上游没有返回内容时，向客户端返回明确错误。",
      ],
      [
        "compatibility_mode_enabled",
        "兼容模式",
        "bool",
        "保持当前客户端兼容处理。",
      ],
    ],
  },
  {
    title: "连接与代理",
    note: "连接地址只在主动保存后改变。",
    fields: [
      ["proxy", "全局代理", "password", "留空使用直连，支持 HTTP 和 SOCKS5。"],
      ["oauth_proxy_url", "OAuth 地址", "url", "令牌获取与刷新端点。"],
      ["googleapis_proxy_url", "Google APIs 地址", "url", "Google API 端点。"],
      [
        "antigravity_api_url",
        "Antigravity 地址",
        "url",
        "模型请求使用的上游端点。",
      ],
    ],
  },
  {
    title: "服务与访问",
    note: "端口、监听地址和凭证目录变更通常需要重启服务。",
    fields: [
      ["host", "监听地址", "text", "例如 0.0.0.0。"],
      ["port", "服务端口", "number", "服务监听端口。", 1, 65535],
      ["credentials_dir", "凭证目录", "text", "本地凭证与数据库存储位置。"],
      ["api_password", "API 访问密码", "password", "用于模型 API 鉴权。"],
      ["panel_password", "面板密码", "password", "用于控制面板登录。"],
    ],
  },
  {
    title: "保活设置",
    note: "只在配置地址后启用保活请求。",
    fields: [
      ["keepalive_url", "保活地址", "url", "留空关闭保活。"],
      [
        "keepalive_interval",
        "保活间隔（秒）",
        "number",
        "保活请求之间的等待时间。",
        5,
        86400,
      ],
    ],
  },
];

export default function Config() {
  const [original, setOriginal] = useState({});
  const [config, setConfig] = useState({});
  const [locked, setLocked] = useState([]);
  const [group, setGroup] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [visible, setVisible] = useState({});
  const [message, setMessage] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    api
      .get("/config/get", { signal: controller.signal })
      .then(({ data }) => {
        setConfig(data.config);
        setOriginal(data.config);
        setLocked(data.env_locked || []);
      })
      .catch((err) => {
        if (err.code !== "ERR_CANCELED")
          setMessage({ error: true, text: errorText(err) });
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);
  const changed = Object.keys(config).filter(
    (key) => config[key] !== original[key] && !locked.includes(key),
  );
  const save = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const changes = Object.fromEntries(
        changed.map((key) => [key, config[key]]),
      );
      await api.post("/config/save", { config: changes });
      if (changes.panel_password)
        localStorage.setItem("panelToken", changes.panel_password);
      const { data } = await api.get("/config/get");
      const matched = Object.entries(changes).every(
        ([key, value]) => data.config[key] === value,
      );
      setConfig(data.config);
      setOriginal(data.config);
      setLocked(data.env_locked || []);
      setMessage({
        error: !matched,
        text: matched
          ? "已保存并读回确认"
          : "读回值与提交值不同，请检查环境变量或其他配置操作",
      });
    } catch (err) {
      setMessage({ error: true, text: errorText(err) });
    } finally {
      setSaving(false);
    }
  };
  if (loading)
    return (
      <div className="empty-state">
        <Loader2 className="animate-spin" size={24} />
        正在读取配置
      </div>
    );
  return (
    <div className="space-y-5">
      {message && (
        <div
          role="status"
          className={`notice ${message.error ? "notice-error" : "notice-success"}`}
        >
          <Check size={16} />
          {message.text}
        </div>
      )}
      <div className="config-layout">
        <nav className="config-nav" aria-label="配置分类">
          {groups.map((item, index) => (
            <button
              key={item.title}
              className={index === group ? "active" : ""}
              aria-current={index === group ? "true" : undefined}
              onClick={() => setGroup(index)}
            >
              {item.title}
            </button>
          ))}
        </nav>
        <div className="card !px-6 !pb-0">
          <h2 className="section-title">{groups[group].title}</h2>
          <p className="config-note mt-2">{groups[group].note}</p>
          {groups[group].fields.map(
            ([key, label, type, note, min, max, step]) => (
              <div className="config-field" key={key}>
                <div>
                  <label htmlFor={`field-${key}`} className="config-label">
                    {label}
                  </label>
                  <p className="config-note">{note}</p>
                  {locked.includes(key) && (
                    <span className="inline-flex items-center gap-1 mt-1 text-[10px] text-amber-400">
                      <LockKeyhole size={10} />
                      环境变量已锁定
                    </span>
                  )}
                </div>
                <div>
                  {type === "bool" ? (
                    <button
                      id={`field-${key}`}
                      type="button"
                      role="switch"
                      aria-checked={!!config[key]}
                      aria-label={label}
                      className="switch"
                      disabled={locked.includes(key) || saving}
                      onClick={() =>
                        setConfig({ ...config, [key]: !config[key] })
                      }
                    >
                      <span />
                    </button>
                  ) : (
                    <div className="relative">
                      <input
                        id={`field-${key}`}
                        className={`input ${type === "password" ? "pr-10" : ""}`}
                        type={
                          type === "password" && visible[key] ? "text" : type
                        }
                        value={config[key] ?? ""}
                        min={min}
                        max={max}
                        step={step || (type === "number" ? 1 : undefined)}
                        disabled={locked.includes(key) || saving}
                        autoComplete="off"
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            [key]:
                              type === "number" && e.target.value !== ""
                                ? Number(e.target.value)
                                : e.target.value,
                          })
                        }
                      />
                      {type === "password" && (
                        <button
                          className="icon-button absolute right-1 top-1"
                          aria-label={
                            visible[key] ? `隐藏${label}` : `显示${label}`
                          }
                          onClick={() =>
                            setVisible({ ...visible, [key]: !visible[key] })
                          }
                        >
                          {visible[key] ? (
                            <EyeOff size={14} />
                          ) : (
                            <Eye size={14} />
                          )}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ),
          )}
        </div>
      </div>
      <div className="flex items-center justify-end gap-4 sticky bottom-4 p-3 rounded-xl border border-dark-700 bg-dark-900/95 backdrop-blur">
        <span className="text-xs text-gray-500">
          {changed.length ? `${changed.length} 项未保存` : "没有未保存的更改"}
        </span>
        <button
          className="btn btn-secondary"
          disabled={!changed.length || saving}
          onClick={() => {
            setConfig(original);
            setMessage(null);
          }}
        >
          撤销更改
        </button>
        <button
          className="btn btn-primary"
          disabled={!changed.length || saving}
          onClick={save}
        >
          {saving ? (
            <Loader2 className="animate-spin" size={15} />
          ) : (
            <Save size={15} />
          )}
          保存配置
        </button>
      </div>
    </div>
  );
}
