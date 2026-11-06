import { useEffect, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  Loader2,
  RefreshCw,
} from "lucide-react";
import api, { downloadBlob, errorText } from "../api";

export default function MarkedPrompts() {
  const [data, setData] = useState({ items: [], total: 0 });
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setSelected(new Set());
    api
      .get("/marked-prompts", {
        params: { offset: page * 30, limit: 30 },
        signal: controller.signal,
      })
      .then(({ data }) => setData(data))
      .catch((err) => {
        if (err.code !== "ERR_CANCELED") setError(errorText(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [page, revision]);
  const download = async (id) => {
    setBusy(true);
    try {
      const response = id
        ? await api.get(`/marked-prompts/${id}/download`, {
            responseType: "blob",
          })
        : await api.post(
            "/marked-prompts/download",
            { ids: [...selected] },
            { responseType: "blob" },
          );
      downloadBlob(
        response.data,
        id ? `prompt-${id.slice(0, 12)}.txt` : "marked-prompts.zip",
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-5">
      <div className="flex justify-between gap-3 items-center">
        <p className="text-xs text-gray-500">
          共 {data.total} 条记录 · 默认只展示摘要
        </p>
        <div className="flex gap-2">
          <button
            className="btn btn-secondary"
            onClick={() => setRevision((v) => v + 1)}
            disabled={loading}
          >
            <RefreshCw size={14} />
            刷新
          </button>
          <button
            className="btn btn-primary"
            onClick={() => download()}
            disabled={!selected.size || busy}
          >
            <Download size={14} />
            下载选中{selected.size ? ` (${selected.size})` : ""}
          </button>
        </div>
      </div>
      {error && (
        <div className="notice notice-error" role="alert">
          {error}
        </div>
      )}
      <div className="table-container">
        <table>
          <thead>
            <tr>
              <th className="w-10">
                <input
                  aria-label="选择本页提示词"
                  type="checkbox"
                  checked={
                    data.items.length > 0 && selected.size === data.items.length
                  }
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? new Set(data.items.map((item) => item.id))
                        : new Set(),
                    )
                  }
                />
              </th>
              <th>提示词摘要</th>
              <th>模型</th>
              <th>字符数</th>
              <th>记录时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <tr key={item.id}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`选择提示词 ${item.id.slice(0, 8)}`}
                    checked={selected.has(item.id)}
                    onChange={() =>
                      setSelected((prev) => {
                        const next = new Set(prev);
                        next.has(item.id)
                          ? next.delete(item.id)
                          : next.add(item.id);
                        return next;
                      })
                    }
                  />
                </td>
                <td className="max-w-xs">
                  <p className="truncate text-xs">{item.preview}</p>
                  <span className="text-[10px] font-mono text-gray-500">
                    {item.id.slice(0, 12)}
                  </span>
                </td>
                <td className="text-xs text-gray-400">{item.model}</td>
                <td className="text-xs text-gray-500">
                  {item.chars.toLocaleString()}
                </td>
                <td className="text-xs text-gray-500 whitespace-nowrap">
                  {new Date(item.created_at * 1000).toLocaleString("zh-CN")}
                </td>
                <td>
                  <button
                    className="icon-button"
                    aria-label="下载提示词"
                    disabled={busy}
                    onClick={() => download(item.id)}
                  >
                    <Download size={15} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!data.items.length && (
          <div className="empty-state">
            {loading ? <Loader2 className="animate-spin" /> : <FileText />}
            <span>{loading ? "正在加载" : "暂无标记提示词"}</span>
          </div>
        )}
        <div className="pagination">
          <span>{data.total} 条记录</span>
          <div className="flex items-center gap-3">
            <button
              aria-label="上一页"
              disabled={!page || loading}
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={14} />
            </button>
            {page + 1} / {Math.max(1, Math.ceil(data.total / 30))}
            <button
              aria-label="下一页"
              disabled={(page + 1) * 30 >= data.total || loading}
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
