import { useEffect, useRef, useState } from "react";
import { Download, Play, Square, Terminal, X } from "lucide-react";
import api, { downloadBlob, errorText } from "../api";
import { getBasePath } from "../basePath";

export default function RuntimeLogs() {
  const [lines, setLines] = useState([]);
  const [connected, setConnected] = useState(false);
  const [message, setMessage] = useState("");
  const [filter, setFilter] = useState("all");
  const [capture, setCapture] = useState({});
  const socket = useRef(null);
  useEffect(() => {
    api
      .get("/config/request-capture/status")
      .then(({ data }) => setCapture(data.status))
      .catch((err) => setMessage(errorText(err)));
    return () => {
      socket.current?.close();
    };
  }, []);
  const connect = () => {
    if (socket.current && socket.current.readyState < 2) return;
    setMessage("");
    if (import.meta.env.VITE_PANEL_PREVIEW === "1") {
      setConnected(true);
      setLines([
        "19:30:00 [INFO] 本地预览日志，不连接线上服务。",
        "19:30:01 [INFO] SQLite 读取完成，耗时 2.4 ms",
        "19:30:02 [INFO] 首段响应收到，耗时 1.2 s",
      ]);
      return;
    }
    const url = new URL(`${getBasePath()}logs/stream`, window.location.origin);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("token", localStorage.getItem("panelToken") || "");
    const ws = new WebSocket(url);
    socket.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onmessage = (event) =>
      setLines((previous) => [...previous, event.data].slice(-500));
    ws.onerror = () => setMessage("日志连接失败，请检查服务日志是否已开启。");
    ws.onclose = () => setConnected(false);
  };
  const download = async (path, filename) => {
    try {
      downloadBlob(
        (await api.get(path, { responseType: "blob" })).data,
        filename,
      );
    } catch (err) {
      setMessage(errorText(err));
    }
  };
  const captureAction = async (action) => {
    try {
      setCapture(
        (
          await api.post(
            `/config/request-capture/${action}`,
            action === "start" ? { capture_type: "400", target_count: 3 } : {},
          )
        ).data.status,
      );
    } catch (err) {
      setMessage(errorText(err));
    }
  };
  const shown =
    filter === "all" ? lines : lines.filter((line) => line.includes(filter));
  return (
    <div className="space-y-5">
      <div className="toolbar">
        <button
          className={`btn ${connected ? "btn-secondary" : "btn-primary"}`}
          onClick={() => {
            if (connected) {
              socket.current?.close();
              setConnected(false);
            } else connect();
          }}
        >
          {connected ? <Square size={14} /> : <Play size={14} />}
          {connected ? "断开日志流" : "连接日志流"}
        </button>
        <button
          className="btn btn-secondary"
          onClick={() => download("/logs/download", "an-runtime.log")}
        >
          <Download size={14} />
          下载日志
        </button>
        <button className="btn btn-secondary" onClick={() => setLines([])}>
          <X size={14} />
          清空视图
        </button>
        <select
          aria-label="日志级别"
          className="input !w-auto ml-auto"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        >
          {["all", "INFO", "WARNING", "ERROR"].map((level) => (
            <option value={level} key={level}>
              {level === "all" ? "全部级别" : level}
            </option>
          ))}
        </select>
      </div>
      {message && (
        <div role="alert" className="notice notice-error">
          {message}
        </div>
      )}
      <div className="rounded-xl border border-dark-700 bg-[#0b0e16] overflow-hidden">
        <div className="flex items-center gap-2 border-b border-dark-700 px-4 py-3 text-xs text-gray-500">
          <Terminal size={14} />
          <span>运行日志</span>
          <span className="ml-auto">
            {connected ? "已连接" : "未连接"} · 最多显示 500 行
          </span>
        </div>
        <pre className="p-5 h-[440px] overflow-auto text-[11px] leading-7 text-gray-400 whitespace-pre-wrap break-all">
          {shown.length ? shown.join("\n") : "连接日志流后显示内容。"}
        </pre>
      </div>
      <section className="card">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div>
            <h2 className="section-title">请求体排查</h2>
            <p className="config-note mt-2">
              手动捕捉 3 个 HTTP 400 请求，保留现有排查功能。
            </p>
          </div>
          <div className="toolbar">
            <button
              className="btn btn-secondary"
              onClick={() => captureAction(capture.active ? "stop" : "start")}
            >
              {capture.active ? "停止捕捉" : "开始捕捉"}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() =>
                download(
                  "/config/request-capture/download",
                  "request-capture.zip",
                )
              }
            >
              <Download size={14} />
              下载
            </button>
          </div>
        </div>
        <p className="mt-3 text-xs text-gray-500">
          {capture.active ? "捕捉进行中" : "未运行"} · 已捕捉{" "}
          {capture.captured_count || 0} / {capture.target_count || 3}
        </p>
      </section>
    </div>
  );
}
