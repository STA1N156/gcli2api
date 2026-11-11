import { useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Download,
  KeyRound,
  Loader2,
  MoreHorizontal,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Snowflake,
  Wallet,
  X,
} from "lucide-react";
import api, { downloadBlob, errorText } from "../api";
import Modal from "../components/Modal";
import Select from "../components/Select";
import QuotaSummary, { QuotaWindows } from "../components/QuotaSummary";

const dateText = (value) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
const labels = {
  enable: "启用",
  disable: "停用",
  delete: "删除",
  enable_credit: "开启信用额度",
  disable_credit: "关闭信用额度",
};

export default function Credentials() {
  const [data, setData] = useState({ items: [], total: 0, stats: {} });
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [status, setStatus] = useState("all");
  const [tier, setTier] = useState("all");
  const [cooldown, setCooldown] = useState("all");
  const [errorCode, setErrorCode] = useState("all");
  const [search, setSearch] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [dialog, setDialog] = useState(null);
  const [menu, setMenu] = useState(null);
  const [menuAbove, setMenuAbove] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 30000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setMenu(null);
    setSelected(new Set());
    api
      .get("/creds/status", {
        signal: controller.signal,
        params: {
          offset: (page - 1) * size,
          limit: size,
          status_filter: status,
          tier_filter: tier,
          cooldown_filter: cooldown,
          error_code_filter: errorCode,
        },
      })
      .then(({ data: result }) => setData(result))
      .catch((err) => {
        if (err.code !== "ERR_CANCELED")
          setNotice({ error: true, text: errorText(err) });
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [page, size, status, tier, cooldown, errorCode, revision]);
  useEffect(() => {
    if (!menu) return;
    const close = (e) => {
      if (!e.target.closest("[data-row-menu]")) setMenu(null);
    };
    const escape = (e) => {
      if (e.key === "Escape") setMenu(null);
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", escape);
    };
  }, [menu]);
  const refresh = () => setRevision((v) => v + 1);
  const act = async (action, filenames) => {
    setBusy(true);
    setMenu(null);
    try {
      const { data: result } = await api.post(
        filenames.length === 1 ? "/creds/action" : "/creds/batch-action",
        filenames.length === 1
          ? { filename: filenames[0], action }
          : { filenames, action },
      );
      setNotice({ error: !!result.errors?.length, text: result.message });
      setDialog(null);
      refresh();
    } catch (err) {
      setNotice({ error: true, text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };
  const askAction = (action, filenames) => {
    setMenu(null);
    setDialog({
      title: `确认${labels[action]}`,
      text: `将${labels[action]} ${filenames.length} 个凭证。${action === "delete" ? "删除前请确认已有备份。" : ""}`,
      action: () => act(action, filenames),
      danger: action === "delete",
    });
  };
  const inspect = async (item, type) => {
    setBusy(true);
    setMenu(null);
    const names = {
      quota: "剩余额度",
      errors: "错误详情",
      detail: "凭证详情",
      test: "测试结果",
      "verify-project": "项目检验",
    };
    try {
      const method = ["test", "verify-project"].includes(type) ? "post" : "get";
      const { data: result } = await api[method](
        `/creds/${type}/${encodeURIComponent(item.filename)}`,
      );
      setDialog({
        title: `${names[type]} · ${item.user_email || item.filename}`,
        result,
        quota: type === "quota",
      });
      if (method === "post") refresh();
    } catch (err) {
      setNotice({ error: true, text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };
  const batchTest = async () => {
    const filenames = [...selected];
    const queue = filenames.values();
    const results = [];
    setBusy(true);
    setMenu(null);
    const progress = () =>
      setDialog({
        title: "批量校验",
        text: `正在校验 ${results.length} / ${filenames.length} 个凭证…`,
      });
    progress();
    try {
      await Promise.all(
        Array.from({ length: Math.min(4, filenames.length) }, async () => {
          for (const filename of queue) {
            let outcome;
            try {
              const { data: result, status } = await api.post(
                `/creds/test/${encodeURIComponent(filename)}`,
                null,
                // 上游的 401、429 也是校验结果，交给本批次汇总。
                { validateStatus: () => true },
              );
              outcome = {
                filename,
                status: result.success && status === 429
                  ? "限流"
                  : result.success && status >= 200 && status < 300
                    ? "通过"
                    : "失败",
                message: result.detail || result.message || `HTTP ${status}`,
              };
            } catch (err) {
              outcome = { filename, status: "失败", message: errorText(err) };
            }
            results.push(outcome);
            progress();
          }
        }),
      );
      const count = (status) => results.filter((item) => item.status === status).length;
      const text = `通过 ${count("通过")} 个，限流 ${count("限流")} 个，失败 ${count("失败")} 个。`;
      setNotice({ error: count("失败") > 0, text: `批量校验完成：${text}` });
      setDialog({ title: "批量校验结果", text, testResults: results });
    } finally {
      setBusy(false);
      refresh();
    }
  };
  const download = async (item) => {
    setBusy(true);
    setMenu(null);
    try {
      downloadBlob(
        (
          await api.get(
            item
              ? `/creds/download/${encodeURIComponent(item.filename)}`
              : "/creds/download-all",
            { responseType: "blob" },
          )
        ).data,
        item?.filename || "antigravity-credentials.zip",
      );
    } catch (err) {
      setNotice({ error: true, text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };
  const visible = data.items.filter((item) =>
    `${item.user_email || ""} ${item.filename}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const toggle = (key) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  const allSelected =
    visible.length > 0 && visible.every((item) => selected.has(item.filename));
  const filterCount = [status, tier, cooldown, errorCode].filter(
    (value) => value !== "all",
  ).length;
  const stats = [
    {
      label: "全部凭证",
      value: data.stats.total,
      icon: KeyRound,
      caption: "已导入的全部凭证",
      style: "",
    },
    {
      label: "正常凭证",
      value: data.stats.normal,
      icon: ShieldCheck,
      caption: "无异常状态的凭证",
      style: "stat-healthy",
    },
    {
      label: "异常凭证",
      value: data.stats.abnormal,
      icon: AlertCircle,
      caption: "需要检查或重新授权",
      style: "stat-warning",
    },
    {
      label: "筛选结果",
      value: data.total,
      icon: CheckCircle2,
      caption: "当前筛选条件下的凭证",
      style: "stat-purple",
    },
  ];
  return (
    <div className="space-y-5">
      <div className="stats-grid">
        {stats.map(({ label, value, icon: Icon, caption, style }) => (
          <section className={`stat-card ${style}`} key={label}>
            <div className="stat-top">
              <span>{label}</span>
              <span className="stat-icon">
                <Icon size={17} strokeWidth={1.7} />
              </span>
            </div>
            <div className="stat-value">
              {value === undefined ? "—" : value.toLocaleString()}
            </div>
            <div className="stat-caption">{caption}</div>
          </section>
        ))}
      </div>
      <QuotaSummary />
      {notice && (
        <div
          role="status"
          className={`notice ${notice.error ? "notice-error" : "notice-success"}`}
        >
          {notice.error ? <AlertCircle size={16} /> : <Check size={16} />}
          <span className="flex-1 whitespace-pre-wrap">{notice.text}</span>
          <button aria-label="关闭提示" onClick={() => setNotice(null)}>
            <X size={15} />
          </button>
        </div>
      )}
      <div className="flex items-center justify-between gap-3 pt-3">
        <h2 className="section-title">
          凭证列表{" "}
          <span className="ml-2 text-gray-500 text-xs font-normal">
            {data.total.toLocaleString()}
          </span>
        </h2>
        <div className="flex gap-2">
          <button
            className="btn btn-secondary !text-xs !px-3 !py-2"
            onClick={() => download()}
            disabled={busy}
          >
            <Download size={14} />
            导出
          </button>
          <button
            className="btn btn-secondary !text-xs !px-3 !py-2"
            onClick={refresh}
            disabled={loading || busy}
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            刷新
          </button>
        </div>
      </div>
      <div className="toolbar credential-filters">
        <label className="search-field flex-1">
          <Search size={16} />
          <input
            aria-label="搜索当前页凭证"
            placeholder="搜索当前页邮箱或文件名"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <button
          className={`btn btn-secondary mobile-filter-toggle ${filterCount ? "text-purple-300" : ""}`}
          aria-label="筛选凭证"
          aria-expanded={filtersOpen}
          onClick={() => setFiltersOpen(!filtersOpen)}
        >
          <SlidersHorizontal size={17} />
          筛选{filterCount > 0 && <span>{filterCount}</span>}
        </button>
        <div className={`filter-options ${filtersOpen ? "is-open" : ""}`}>
          <Select
            className="filter-select"
            value={status}
            onChange={(value) => {
              setStatus(value);
              setPage(1);
            }}
            options={[
              { value: "all", label: "全部状态" },
              { value: "normal", label: "正常" },
              { value: "abnormal", label: "异常" },
              { value: "claude_abnormal", label: "Claude 异常" },
              { value: "gemini_abnormal", label: "Gemini 异常" },
            ]}
          />
          <Select
            className="filter-select"
            value={tier}
            onChange={(value) => {
              setTier(value);
              setPage(1);
            }}
            options={[
              { value: "all", label: "全部订阅" },
              { value: "pro", label: "Pro" },
              { value: "ultra", label: "Ultra" },
              { value: "ultra_lite", label: "Ultra Lite" },
              { value: "free", label: "Free" },
            ]}
          />
          <Select
            className="filter-select"
            value={cooldown}
            onChange={(value) => {
              setCooldown(value);
              setPage(1);
            }}
            options={[
              { value: "all", label: "全部冷却" },
              { value: "in_cooldown", label: "冷却中" },
              { value: "no_cooldown", label: "未冷却" },
            ]}
          />
          <Select
            className="filter-select"
            value={errorCode}
            onChange={(value) => {
              setErrorCode(value);
              setPage(1);
            }}
            options={[
              { value: "all", label: "全部错误码" },
              ...[400, 401, 403, 429, 500, 503].map((code) => ({
                value: String(code),
                label: `HTTP ${code}`,
              })),
            ]}
          />
        </div>
      </div>
      {selected.size > 0 && (
        <div className="toolbar batch-toolbar text-xs text-purple-300 bg-purple-500/5 rounded-lg px-4 py-3 border border-purple-500/20">
          <span className="mr-auto">已选择 {selected.size} 个凭证</span>
          <button
            className="btn btn-secondary !text-xs !py-1.5"
            title="使用 Gemini 3.5 Flash Lite 测试选中的凭证"
            disabled={busy || loading}
            onClick={batchTest}
          >
            <Play size={14} />
            批量校验
          </button>
          {["enable", "disable", "delete"].map((action) => (
            <button
              className={`btn !text-xs !py-1.5 ${action === "delete" ? "btn-danger" : "btn-secondary"}`}
              key={action}
              disabled={busy || loading}
              onClick={() => askAction(action, [...selected])}
            >
              {labels[action]}
            </button>
          ))}
        </div>
      )}
      <div
        className="table-container credential-table-wrap"
        aria-busy={loading}
      >
        <table className="credential-table">
          <thead>
            <tr>
              <th className="!pr-0 w-10">
                <input
                  type="checkbox"
                  aria-label="选择当前页可见凭证"
                  checked={allSelected}
                  onChange={() =>
                    setSelected(
                      allSelected
                        ? new Set()
                        : new Set(visible.map((item) => item.filename)),
                    )
                  }
                  disabled={loading}
                />
              </th>
              <th>凭证</th>
              <th>订阅</th>
              <th>状态</th>
              <th>最近成功</th>
              <th className="!text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((item) => {
              const cooling = Object.values(item.model_cooldowns || {}).some(
                (until) => until > now,
              );
              const abnormal = item.disabled || !!item.error_codes?.length;
              return (
                <tr
                  key={item.filename}
                  className={
                    selected.has(item.filename) ? "bg-purple-500/5" : ""
                  }
                >
                  <td className="!pr-0">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${item.user_email || item.filename}`}
                      checked={selected.has(item.filename)}
                      onChange={() => toggle(item.filename)}
                      disabled={loading || busy}
                    />
                  </td>
                  <td>
                    <div className="flex items-center gap-3">
                      <span className="credential-avatar">
                        {(item.user_email || item.filename)
                          .slice(0, 2)
                          .toUpperCase()}
                      </span>
                      <div>
                        <div
                          className="credential-email"
                          title={item.user_email || item.filename}
                        >
                          {item.user_email || item.filename}
                        </div>
                        <div className="credential-file" title={item.filename}>
                          {item.filename}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span
                      className={`badge ${["ultra", "ultra_lite"].includes(item.tier) ? "text-purple-300 border-purple-500/20 bg-purple-500/10" : "text-gray-400 border-dark-600"}`}
                    >
                      {item.tier === "ultra"
                        ? "Ultra"
                        : item.tier === "ultra_lite"
                          ? "Ultra Lite"
                          : item.tier === "free"
                            ? "Free"
                            : item.tier === "pro"
                              ? "Pro"
                              : item.tier || "未知"}
                    </span>
                    {item.enable_credit && (
                      <div className="text-[10px] text-gray-500 mt-1">
                        信用额度
                      </div>
                    )}
                  </td>
                  <td>
                    <div className="space-y-1">
                      <span
                        className={`inline-flex items-center gap-1.5 text-xs ${item.disabled ? "text-red-400" : abnormal ? "text-amber-400" : "text-emerald-400"}`}
                      >
                        <span className="h-1.5 w-1.5 rounded-full bg-current" />
                        {item.disabled ? "已停用" : abnormal ? "异常" : "正常"}
                      </span>
                      {cooling && (
                        <div className="flex items-center gap-1 text-[10px] text-blue-400">
                          <Snowflake size={11} />
                          冷却中
                        </div>
                      )}
                    </div>
                  </td>
                  <td className="text-gray-500 !text-xs whitespace-nowrap">
                    <span className="inline-flex items-center gap-1.5">
                      <Clock3 size={12} />
                      {dateText(item.last_success)}
                    </span>
                  </td>
                  <td>
                    <div className="row-actions justify-end">
                      <button
                        className="icon-button"
                        title="使用 Gemini 3.5 Flash Lite 测试"
                        aria-label={`测试 ${item.user_email || item.filename}`}
                        onClick={() => inspect(item, "test")}
                        disabled={busy || loading}
                      >
                        <Play size={15} />
                      </button>
                      <button
                        className="icon-button"
                        title="查看额度"
                        aria-label={`查看 ${item.user_email || item.filename} 的额度`}
                        onClick={() => inspect(item, "quota")}
                        disabled={busy || loading}
                      >
                        <Wallet size={15} />
                      </button>
                      <div className="relative" data-row-menu>
                        <button
                          className="icon-button"
                          aria-label={`更多操作 ${item.user_email || item.filename}`}
                          aria-expanded={menu === item.filename}
                          onClick={(event) => {
                            setMenuAbove(
                              window.innerHeight -
                                event.currentTarget.getBoundingClientRect()
                                  .bottom <
                                340,
                            );
                            setMenu(
                              menu === item.filename ? null : item.filename,
                            );
                          }}
                          disabled={busy || loading}
                        >
                          <MoreHorizontal size={18} />
                        </button>
                        {menu === item.filename && (
                          <div
                            className={`menu-popover ${menuAbove ? "bottom-full mb-1" : "top-full mt-1"}`}
                          >
                            {["detail", "errors", "verify-project"].map(
                              (type) => (
                                <button
                                  key={type}
                                  onClick={() => inspect(item, type)}
                                >
                                  {
                                    {
                                      detail: "查看凭证",
                                      errors: "错误详情",
                                      "verify-project": "检验项目",
                                    }[type]
                                  }
                                </button>
                              ),
                            )}
                            <button onClick={() => download(item)}>
                              导出凭证
                            </button>
                            <button
                              onClick={() =>
                                askAction(
                                  item.disabled ? "enable" : "disable",
                                  [item.filename],
                                )
                              }
                            >
                              {item.disabled ? "启用" : "停用"}
                            </button>
                            <button
                              onClick={() =>
                                askAction(
                                  item.enable_credit
                                    ? "disable_credit"
                                    : "enable_credit",
                                  [item.filename],
                                )
                              }
                            >
                              {item.enable_credit
                                ? "关闭信用额度"
                                : "开启信用额度"}
                            </button>
                            <button
                              className="text-red-400"
                              onClick={() =>
                                askAction("delete", [item.filename])
                              }
                            >
                              删除凭证
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!visible.length && (
          <div className="empty-state">
            {loading ? (
              <Loader2 size={24} className="animate-spin" />
            ) : (
              <KeyRound size={28} strokeWidth={1.4} />
            )}
            <span>{loading ? "正在加载凭证" : "没有符合条件的凭证"}</span>
          </div>
        )}
        <div className="pagination">
          <div className="flex items-center gap-2">
            <span className="hidden sm:inline">每页</span>
            <select
              className="bg-transparent border border-dark-600 rounded px-1 py-1"
              aria-label="每页条数"
              value={size}
              onChange={(e) => {
                setSize(Number(e.target.value));
                setPage(1);
              }}
            >
              {[20, 50, 100, 200, 500, 1000].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            <span>共 {data.total.toLocaleString()} 条</span>
          </div>
          <div className="flex items-center gap-3">
            <button
              aria-label="上一页"
              disabled={page <= 1 || loading}
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={14} />
            </button>
            <span>
              {page} / {Math.max(1, Math.ceil(data.total / size))}
            </span>
            <button
              aria-label="下一页"
              disabled={page * size >= data.total || loading}
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      </div>
      {dialog && (
        <Modal
          title={dialog.title}
          type={dialog.danger ? "error" : "info"}
          onClose={() => !busy && setDialog(null)}
          footer={
            dialog.action ? (
              <>
                <button
                  className="btn btn-secondary"
                  disabled={busy}
                  onClick={() => setDialog(null)}
                >
                  取消
                </button>
                <button
                  className={`btn ${dialog.danger ? "btn-danger" : "btn-primary"}`}
                  disabled={busy}
                  onClick={dialog.action}
                >
                  {busy ? <Loader2 size={14} className="animate-spin" /> : null}
                  确认
                </button>
              </>
            ) : undefined
          }
        >
          {dialog.testResults ? (
            <div className="space-y-3">
              <p>{dialog.text}</p>
              <div className="space-y-2 text-xs break-all">
                {dialog.testResults.map((item) => (
                  <div key={item.filename}>
                    {item.filename} · {item.status}
                    {item.status === "失败" && `：${item.message}`}
                  </div>
                ))}
              </div>
            </div>
          ) : dialog.text ||
            (dialog.quota ? (
              <QuotaWindows windows={dialog.result.windows} />
            ) : (
              <pre className="text-xs whitespace-pre-wrap">
                {JSON.stringify(dialog.result, null, 2)}
              </pre>
            ))}
        </Modal>
      )}
    </div>
  );
}
