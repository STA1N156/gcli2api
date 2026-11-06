import { useState } from "react";
import { Check, Copy, ExternalLink, Github, RefreshCw } from "lucide-react";
import { getBasePath } from "../basePath";

export default function About({ version, onCheckUpdate, checkingUpdate }) {
  const [copied, setCopied] = useState(null);
  const [error, setError] = useState("");
  const copy = async (value) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(value);
    } catch {
      setError("复制失败，请手动选择地址复制。");
    }
  };
  const base = window.location.origin + getBasePath() + "antigravity/";
  return (
    <div className="space-y-5 max-w-4xl">
      <section className="card">
        <h2 className="section-title">API 接入</h2>
        <div className="divide-y divide-dark-700 mt-4">
          {[
            ["OpenAI", "v1/chat/completions"],
            ["Gemini", "v1beta/models"],
            ["Anthropic", "v1/messages"],
          ].map(([label, path]) => (
            <div className="flex items-center gap-3 py-4" key={path}>
              <span className="w-24 text-xs text-gray-400 shrink-0">
                {label}
              </span>
              <code className="text-xs text-gray-300 flex-1 break-all">
                {base}
                {path}
              </code>
              <button
                className="icon-button"
                aria-label={`复制 ${label} 地址`}
                onClick={() => copy(base + path)}
              >
                {copied === base + path ? (
                  <Check size={15} />
                ) : (
                  <Copy size={15} />
                )}
              </button>
            </div>
          ))}
        </div>
        {error && <p className="text-red-400 text-xs">{error}</p>}
      </section>
      <section className="card">
        <div className="flex items-center justify-between">
          <h2 className="section-title">版本信息</h2>
          <button
            className="btn btn-secondary !text-xs"
            disabled={checkingUpdate}
            onClick={onCheckUpdate}
          >
            <RefreshCw
              size={14}
              className={checkingUpdate ? "animate-spin" : ""}
            />
            检查上游更新
          </button>
        </div>
        <dl className="grid sm:grid-cols-2 gap-6 mt-6 text-xs">
          <div>
            <dt className="text-gray-500 mb-2">Antigravity CLI</dt>
            <dd>{version?.antigravity_cli_version || "—"}</dd>
          </div>
          <div>
            <dt className="text-gray-500 mb-2">当前构建</dt>
            <dd>{version?.version || "—"}</dd>
          </div>
          <div>
            <dt className="text-gray-500 mb-2">构建时间</dt>
            <dd>{version?.date || "—"}</dd>
          </div>
          <div>
            <dt className="text-gray-500 mb-2">更新说明</dt>
            <dd>{version?.message || "—"}</dd>
          </div>
        </dl>
        {version?.check_update && (
          <p className="mt-5 text-xs text-purple-300">
            {version.has_update
              ? `上游最新版本：${version.latest_version}`
              : "与上游版本一致"}
          </p>
        )}
        {version?.update_error && (
          <p className="text-amber-400 text-xs mt-4">{version.update_error}</p>
        )}
      </section>
      <section className="card">
        <h2 className="section-title">关于</h2>
        <p className="text-xs text-gray-400 leading-7 mt-3">
          控制面板基于上游 AGY2API React
          界面改进，保留本分支的凭证管理、额度汇总、标记提示词及运行日志功能。遵循项目
          CNC-1.0 许可证，仅供学习研究使用。
        </p>
        <a
          className="inline-flex items-center gap-2 text-purple-300 text-xs mt-4"
          href="https://github.com/su-kaka/agy2api"
          target="_blank"
          rel="noreferrer"
        >
          <Github size={15} />
          上游项目
          <ExternalLink size={12} />
        </a>
      </section>
    </div>
  );
}
