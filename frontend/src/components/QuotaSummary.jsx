import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp, Loader2, RefreshCw } from "lucide-react";
import api, { errorText } from "../api";

export function QuotaWindows({ windows }) {
  return (
    <div className="quota-grid">
      {["gemini", "claude"].flatMap((family) =>
        ["5h", "7d"].map((period) => {
          const item = Array.isArray(windows)
            ? windows.find((w) => w.family === family && w.period === period)
            : windows?.[family]?.[period];
          const value = item?.remaining_percent;
          const known = typeof value === "number";
          return (
            <div key={family + period}>
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="text-gray-400">
                  {family === "gemini" ? "Gemini" : "Claude"}{" "}
                  <span className="text-gray-500">
                    / {period === "5h" ? "5 小时" : "7 天"}
                  </span>
                </span>
                <strong
                  className={
                    known && value < 20 ? "text-amber-400" : "text-gray-200"
                  }
                >
                  {known ? `${value.toFixed(1)}%` : "—"}
                </strong>
              </div>
              <div className="quota-track">
                <span
                  style={{
                    width: `${known ? Math.max(0, Math.min(100, value)) : 0}%`,
                    background: family === "claude" ? "#66b6ad" : undefined,
                  }}
                />
              </div>
            </div>
          );
        }),
      )}
    </div>
  );
}

export default function QuotaSummary() {
  const [snapshot, setSnapshot] = useState(null);
  const [open, setOpen] = useState(() => window.innerWidth > 800);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const running = starting || !!snapshot?.running;
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    const poll = async () => {
      try {
        const { data } = await api.get(
          "/creds/antigravity-credit-summary-progress",
          { signal: controller.signal },
        );
        setSnapshot(data);
        if (data.running) timer = setTimeout(poll, 1500);
      } catch (err) {
        if (err.code !== "ERR_CANCELED") setError(errorText(err));
      }
    };
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [starting]);
  const refresh = async () => {
    setStarting(true);
    setError("");
    try {
      setSnapshot(
        (
          await api.get("/creds/antigravity-credit-summary", {
            params: { force: true },
          })
        ).data,
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setStarting(false);
    }
  };
  const result = snapshot?.result || snapshot;
  return (
    <section className="card !p-5">
      <div className="flex items-center justify-between gap-3">
        <button
          className="flex items-center gap-2 section-title"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          额度概览{" "}
          {open ? (
            <ChevronUp size={15} className="text-gray-500" />
          ) : (
            <ChevronDown size={15} />
          )}
        </button>
        <div className="flex items-center gap-4">
          <span className="hidden sm:inline text-[10px] text-gray-500">
            {running
              ? `已查询 ${snapshot?.completed || 0} / ${snapshot?.total || 0}`
              : result?.updated_at
                ? `更新于 ${new Date(result.updated_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}`
                : "尚未查询"}
          </span>
          <button
            className="text-xs text-gray-400 hover:text-purple-300 flex items-center gap-1.5"
            onClick={refresh}
            disabled={running}
          >
            {running ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <RefreshCw size={13} />
            )}
            刷新额度
          </button>
        </div>
      </div>
      {open && (
        <div className="mt-6">
          <QuotaWindows windows={result?.model_summaries} />
          {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
        </div>
      )}
    </section>
  );
}
