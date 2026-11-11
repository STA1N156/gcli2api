import { Suspense, lazy, useEffect, useState } from "react";
import {
  Activity,
  ArrowUpRight,
  ChevronsUpDown,
  HelpCircle,
  KeyRound,
  Loader2,
  LogOut,
  Menu,
  Plus,
  Settings2,
  Sparkles,
  Terminal,
  Upload,
  X,
} from "lucide-react";
import api, { errorText } from "./api";

const Login = lazy(() => import("./pages/Login"));
const Credentials = lazy(() => import("./pages/Credentials"));
const OAuth = lazy(() => import("./pages/OAuth"));
const UploadPage = lazy(() => import("./pages/Upload"));
const Config = lazy(() => import("./pages/Config"));
const About = lazy(() => import("./pages/About"));
const RuntimeLogs = lazy(() => import("./pages/RuntimeLogs"));
const preview = import.meta.env.VITE_PANEL_PREVIEW === "1";
const tabs = [
  {
    id: "credentials",
    label: "凭证管理",
    icon: KeyRound,
    component: Credentials,
  },
  { id: "oauth", label: "OAuth 认证", icon: Sparkles, component: OAuth },
  { id: "upload", label: "批量导入", icon: Upload, component: UploadPage },
  { id: "logs", label: "运行日志", icon: Terminal, component: RuntimeLogs },
  { id: "config", label: "配置管理", icon: Settings2, component: Config },
  { id: "about", label: "项目信息", icon: HelpCircle, component: About },
];

export default function App() {
  const [loggedIn, setLoggedIn] = useState(
    () => preview || !!localStorage.getItem("panelToken"),
  );
  const [activeTab, setActiveTab] = useState("credentials");
  const [mobileNav, setMobileNav] = useState(false);
  const [version, setVersion] = useState(null);
  const [checking, setChecking] = useState(false);
  const current = tabs.find((tab) => tab.id === activeTab);
  const Page = current.component;
  useEffect(() => {
    api
      .get("/version/info")
      .then(({ data }) => setVersion(data))
      .catch(() => {});
  }, []);
  const navigate = (id) => {
    setActiveTab(id);
    setMobileNav(false);
  };
  const checkUpdate = async () => {
    setChecking(true);
    try {
      setVersion(
        (await api.get("/version/info", { params: { check_update: true } }))
          .data,
      );
    } catch (error) {
      setVersion((previous) => ({
        ...previous,
        update_error: errorText(error),
      }));
    } finally {
      setChecking(false);
    }
  };
  const fallback = (
    <div className="empty-state">
      <Loader2 className="animate-spin" size={24} />
      <span>正在加载</span>
    </div>
  );
  if (!loggedIn)
    return (
      <Suspense fallback={fallback}>
        <Login onSuccess={() => setLoggedIn(true)} />
      </Suspense>
    );
  return (
    <div className="app-shell">
      {mobileNav && (
        <button
          className="nav-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobileNav(false)}
        />
      )}
      <aside className={`sidebar ${mobileNav ? "is-open" : ""}`}>
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            navigate("credentials");
          }}
        >
          <span className="brand-mark">
            <Sparkles size={23} strokeWidth={1.8} />
          </span>
          <span>
            Antigravity<span className="brand-caption">AN CONSOLE</span>
          </span>
        </a>
        <div className="workspace">
          <span className="workspace-avatar">AN</span>
          <span>
            <strong>AN 工作空间</strong>
            <small>Antigravity API</small>
          </span>
          <ChevronsUpDown size={15} />
        </div>
        <span className="nav-label">工作空间</span>
        <nav aria-label="主导航">
          {tabs.map(({ id, label, icon: Icon }, index) => (
            <button
              key={id}
              aria-current={activeTab === id ? "page" : undefined}
              className={`nav-item ${activeTab === id ? "active" : ""} ${index === 5 ? "nav-divider" : ""}`}
              onClick={() => navigate(id)}
            >
              <Icon size={18} strokeWidth={1.7} />
              <span>{label}</span>
              {activeTab === id && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="version-line">
            <Activity size={15} />
            <span>Antigravity CLI</span>
            <span>{version?.antigravity_cli_version || "—"}</span>
          </div>
          <button
            className="nav-item"
            onClick={() => {
              localStorage.removeItem("panelToken");
              setLoggedIn(false);
            }}
          >
            <LogOut size={17} />
            <span>退出登录</span>
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="flex items-center gap-3">
            <button
              className="icon-button mobile-toggle"
              aria-label="打开导航"
              onClick={() => setMobileNav(!mobileNav)}
            >
              {mobileNav ? <X size={20} /> : <Menu size={20} />}
            </button>
            <span className="breadcrumb">
              工作空间 <span>/</span> <strong>{current.label}</strong>
            </span>
          </div>
          <div className="flex items-center gap-3">
            {preview && (
              <span className="preview-badge">本地预览 · 示例数据</span>
            )}
            <a
              className="icon-button"
              title="上游仓库"
              aria-label="打开上游仓库"
              href="https://github.com/su-kaka/agy2api"
              target="_blank"
              rel="noreferrer"
            >
              <ArrowUpRight size={18} />
            </a>
            <span className="topbar-avatar">AN</span>
          </div>
        </header>
        <main className="main-content">
          <div className="page-heading">
            <div>
              <span className="eyebrow">ANTIGRAVITY</span>
              <h1>{current.label}</h1>
            </div>
            {activeTab === "credentials" && (
              <button
                className="btn btn-primary"
                onClick={() => navigate("oauth")}
              >
                <Plus size={17} />
                添加凭证
              </button>
            )}
          </div>
          <Suspense fallback={fallback}>
            <Page
              version={version}
              onCheckUpdate={checkUpdate}
              checkingUpdate={checking}
            />
          </Suspense>
          <footer className="page-footer">
            <span>AN Console</span>
            <span>基于 AGY2API · {version?.version || "本地开发"}</span>
          </footer>
        </main>
        <nav className="mobile-bottom-nav" aria-label="快捷导航">
          {tabs
            .filter((tab) => ["credentials", "logs", "config"].includes(tab.id))
            .map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                className={activeTab === id ? "active" : ""}
                aria-current={activeTab === id ? "page" : undefined}
                onClick={() => navigate(id)}
              >
                <Icon size={19} />
                <span>{label.replace("管理", "")}</span>
              </button>
            ))}
          <button
            aria-label="更多导航"
            aria-expanded={mobileNav}
            onClick={() => setMobileNav(!mobileNav)}
          >
            <Menu size={19} />
            <span>更多</span>
          </button>
        </nav>
      </div>
    </div>
  );
}
