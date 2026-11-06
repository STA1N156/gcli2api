import axios from "axios";
import { getBasePath } from "./basePath.js";

const api = axios.create({
  // 基径取面板页面所在路径（末尾补斜杠），反代到子路径下时请求仍落在同一子路径内
  baseURL: getBasePath(),
  timeout: 120000,
});

// Explicit local preview only; Vite removes this adapter from production builds.
if (import.meta.env.VITE_PANEL_PREVIEW === "1") {
  api.defaults.adapter = async (config) =>
    (await import("./preview.js")).respond(config);
}

// 请求拦截器 - 添加token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem("panelToken");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 响应拦截器 - 处理错误
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && error.config?.url !== "/auth/login") {
      localStorage.removeItem("panelToken");
      window.location.href = getBasePath();
    }
    return Promise.reject(error);
  },
);

export default api;

export function errorText(error) {
  const data = error.response?.data;
  const detail = data?.detail || data?.error || data?.message || error.message;
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

export function downloadBlob(data, filename) {
  const url = URL.createObjectURL(
    data instanceof Blob ? data : new Blob([data]),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
