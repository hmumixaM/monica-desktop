import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import * as bridge from "./bridge";
import ImportProgress from "./ImportProgress";
import PipelineProgress from "./PipelineProgress";
import {
  defaultConfig,
  normalizeConfig,
  type DesktopConfig,
  type DesktopStatus,
  type ServiceStatus,
} from "./types";

type Tab = "overview" | "sources" | "ai" | "general";
const tabs: { id: Tab; label: string; icon: string }[] = [
  { id: "overview", label: "运行概览", icon: "◉" },
  { id: "sources", label: "数据源", icon: "⇄" },
  { id: "ai", label: "模型服务", icon: "✧" },
  { id: "general", label: "常规", icon: "⚙" },
];
const stateLabels: Record<string, string> = {
  starting: "正在启动",
  running: "运行中",
  ready: "运行中",
  paused: "已暂停",
  stopped: "已停止",
  stopping: "正在停止",
  pausing: "正在暂停",
  error: "需要处理",
  failed: "需要处理",
  disabled: "未启用",
  idle: "等待下一次任务",
  needs_login: "需要登录",
  needs_setup: "等待设置",
  unconfigured: "等待设置",
  missing: "尚未就绪",
  waiting: "等待中",
  connected: "已连接",
  syncing: "正在同步",
  collecting: "正在采集",
  connecting: "正在连接",
  not_configured: "未连接",
  needs_models: "需要人脸模型",
  disconnected: "未连接",
  completed: "已完成",
  backoff: "等待重试",
  unknown: "等待状态",
};
const serviceLabels: Record<string, string> = {
  postgres: "数据存储",
  api: "本地服务",
  web: "Web 界面",
  gateway: "浏览器入口",
  scheduler: "任务调度",
  worker: "后台处理",
  "worker-fast": "后台处理",
  "worker-ai": "模型任务",
  "image-ocr": "图片识别",
  "archive-sync": "归档导入",
  "archive-contacts": "微信联系人", "archive-moments": "微信朋友圈", "archive-messages": "微信聊天",
  "archive-media": "微信图片关联", "archive-download": "微信图片下载", "archive-maintenance": "统计与缓存",
  "archive-instagram": "Instagram 资料", "archive-instagram-media": "Instagram 本地图片",
  "worker-db": "统计与索引", "worker-media": "网络图片", "worker-projection": "资料更新",
  "worker-projection-2": "资料更新（并行）",
  "worker-scheduler": "任务调度", "cdc-capture": "历史变化记录", "source-dispatch": "导入任务接收",
  archive_sync: "归档导入",
  chatlog: "微信连接",
  wechat: "微信",
  instagram: "Instagram",
  linkedin: "LinkedIn",
  faces: "照片里的人",
  "linkedin-sync": "LinkedIn 导入",
};
function readableError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
function State({ state }: { state: string }) {
  const tone = /error|failed|needs_|missing/.test(state)
    ? "attention"
    : ["running", "ready", "connected"].includes(state)
      ? "good"
      : "neutral";
  return (
    <span className={`state ${tone}`}>
      <span className="state-dot" aria-hidden="true" />
      {stateLabels[state] ?? state}
    </span>
  );
}
function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <div className="section-heading">
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {children}
    </section>
  );
}
function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </label>
  );
}
function Toggle({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  description?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="toggle-row">
      <span>
        <strong>{label}</strong>
        {description && <span className="hint">{description}</span>}
      </span>
      <input
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="switch" aria-hidden="true" />
    </label>
  );
}
function SourceState({ status }: { status?: ServiceStatus }) {
  if (!status) return <State state="not_configured" />;
  return (
    <div className="source-state">
      <State state={status.state} />
      {status.last_success && (
        <small>
          上次完成{" "}
          {new Date(status.last_success).toLocaleString("zh-CN", {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </small>
      )}
      {status.error && <p className="inline-error">{status.error}</p>}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [config, setConfig] = useState<DesktopConfig>(defaultConfig);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const initialized = useRef(false);
  const mounted = useRef(true);
  const polling = useRef(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [wechatDataKey, setWechatDataKey] = useState("");
  const [wechatImageKey, setWechatImageKey] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const refresh = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    try {
      const next = await bridge.getStatus();
      if (!mounted.current) return;
      setStatus(next);
      setConnectionError("");
      if ((!initialized.current || !dirtyRef.current) && next.config) {
        setConfig(normalizeConfig(next.config));
        initialized.current = true;
      }
    } catch (failure) {
      if (mounted.current) setConnectionError(readableError(failure));
    } finally {
      polling.current = false;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  async function run(
    label: string,
    action: () => Promise<unknown>,
    success = "",
  ) {
    if (busy) return;
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(success);
      await refresh();
    } catch (failure) {
      setError(readableError(failure));
    } finally {
      setBusy(null);
    }
  }
  function updateConfig(update: (previous: DesktopConfig) => DesktopConfig) {
    setConfig(update);
    dirtyRef.current = true;
    setDirty(true);
  }
  async function saveConfig() {
    if (!dirtyRef.current && !apiKey) return;
    const { api_key_set: _keyIsSet, ...ai } = config.ai;
    await bridge.command("save_config", {
      config: {
        ...config,
        ai: { ...ai, ...(apiKey ? { api_key: apiKey } : {}) },
      },
    });
    setApiKey("");
    dirtyRef.current = false;
    setDirty(false);
  }
  async function pickPath(kind: "directory" | "file") {
    const value =
      kind === "directory"
        ? await bridge.chooseDirectory()
        : await bridge.chooseFile();
    if (value)
      updateConfig((previous) => ({
        ...previous,
        wechat: {
          ...previous.wechat,
          [kind === "directory" ? "data_dir" : "config_file"]: value,
        },
      }));
  }
  function onSubmit(
    event: FormEvent,
    label: string,
    action: () => Promise<unknown>,
    success = "",
  ) {
    event.preventDefault();
    void run(label, action, success);
  }
  const state = status?.state ?? "starting";
  const isPaused = state === "paused";
  const canStart = state === "stopped" || state === "error";
  const needsAccount = status?.account_created === false;
  const source = (name: string) =>
    status?.sources?.find((item) => item.name === name);
  const canAct =
    !!status &&
    !busy &&
    !status.operation &&
    !["starting", "stopping", "pausing"].includes(state);

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="设置导航">
        <div className="wordmark">
          <span className="brand-mark" aria-hidden="true">
            m
          </span>
          <span>Monica</span>
        </div>
        <nav>
          {tabs.map((item) => (
            <button
              key={item.id}
              className={`nav-item ${tab === item.id ? "selected" : ""}`}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => setTab(item.id)}
            >
              <span aria-hidden="true">{item.icon}</span>
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <span className="local-dot" />
          在这台 Mac 上运行
        </div>
      </aside>
      <div className="main-column">
        <header className="page-header">
          <div>
            <h1>{tabs.find((item) => item.id === tab)?.label}</h1>
            <State state={state} />
          </div>
          <button
            className="button primary"
            disabled={!status?.web_url || !!busy}
            onClick={() => void run("打开 Monica", bridge.openMonica)}
          >
            打开 Monica <span aria-hidden="true">↗</span>
          </button>
        </header>
        <main>
          <div className="feedback" aria-live="polite" aria-atomic="true">
            {(error || connectionError || status?.error) && (
              <div className="message error">
                <strong>需要处理</strong>
                <p>{error || connectionError || status?.error}</p>
                <button
                  className="text-button"
                  onClick={() => void run("打开日志", bridge.openLogs)}
                >
                  查看日志
                </button>
              </div>
            )}
            {notice && <div className="message success">{notice}</div>}
            {busy && (
              <div className="working" role="status">
                <span className="spinner" aria-hidden="true" />
                {busy}…
              </div>
            )}
          </div>

          <fieldset
            className="settings-content"
            disabled={!!busy || !!status?.operation}
          >
            {tab === "overview" && (
              <>
                <div className="running-summary">
                  <div className="summary-line" />
                  <div>
                    <h2>
                      {state === "running" || state === "ready"
                        ? "Monica 正在后台工作"
                        : isPaused
                          ? "后台任务已暂停"
                          : state === "stopping"
                            ? "正在保存进度并退出"
                            : state === "stopped"
                              ? "本地服务已停止"
                              : "让 Monica 在本机安顿下来"}
                    </h2>
                    <p>
                      {isPaused
                        ? "仍可浏览已有内容。恢复后会继续同步与处理。"
                        : state === "stopping"
                          ? "等待当前任务结束后，应用会自动关闭。"
                          : "关闭这个窗口后，菜单栏中的 Monica 会继续运行。"}
                    </p>
                    <div className="button-row">
                      <button
                        className="button"
                        disabled={!canAct}
                        onClick={() =>
                          void run(
                            isPaused
                              ? "恢复任务"
                              : canStart
                                ? "启动服务"
                                : "暂停任务",
                            () =>
                              bridge.command(
                                isPaused
                                  ? "resume"
                                  : canStart
                                    ? "start"
                                    : "pause",
                              ),
                          )
                        }
                      >
                        {isPaused
                          ? "恢复任务"
                          : canStart
                            ? "启动服务"
                            : "暂停任务"}
                      </button>
                      <button
                        className="text-button"
                        onClick={() => setTab("sources")}
                      >
                        管理数据源
                      </button>
                    </div>
                  </div>
                </div>
                {needsAccount && (
                  <Section
                    title="创建本地账号"
                    description="用这个账号在浏览器中查看你的 Monica。"
                  >
                    <form
                      onSubmit={(event) =>
                        onSubmit(
                          event,
                          "创建账号",
                          async () => {
                            await bridge.command("create_account", {
                              email,
                              password,
                            });
                            setPassword("");
                          },
                          "本地账号已创建，可以打开 Monica 登录。",
                        )
                      }
                    >
                      <Field label="邮箱">
                        <input
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          type="email"
                          autoComplete="username"
                          value={email}
                          onChange={(event) => setEmail(event.target.value)}
                          required
                        />
                      </Field>
                      <Field label="密码" hint="至少 12 个字符。">
                        <input
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          type="password"
                          autoComplete="new-password"
                          value={password}
                          onChange={(event) => setPassword(event.target.value)}
                          minLength={12}
                          required
                        />
                      </Field>
                      <button
                        className="button primary"
                        disabled={!canAct}
                        type="submit"
                      >
                        创建账号
                      </button>
                    </form>
                  </Section>
                )}
                {status && <ImportProgress status={status} />}
                {status && <PipelineProgress status={status} />}
                <Section
                  title="数据源"
                  description="每个平台的登录与同步状态会显示在这里。"
                >
                  <div className="status-list">
                    {[
                      { name: "wechat", label: "微信" },
                      { name: "instagram", label: "Instagram" },
                      { name: "linkedin", label: "LinkedIn" },
                      { name: "faces", label: "照片里的人" },
                    ].map((item) => (
                      <div key={item.name} className="status-row">
                        <span>{item.label}</span>
                        <SourceState status={source(item.name)} />
                      </div>
                    ))}
                  </div>
                </Section>
                <details className="service-details">
                  <summary>本地服务状态</summary>
                  <div className="status-list">
                    {status?.services?.map((service) => (
                      <div className="status-row" key={service.name}>
                        <span>
                          {serviceLabels[service.name] ?? service.name}
                        </span>
                        <SourceState status={service} />
                      </div>
                    ))}
                  </div>
                </details>
              </>
            )}

            {tab === "sources" && (
              <>
                <Section
                  title="微信"
                  description="选择此 Mac 上的微信数据目录，再连接已有配置或填写解密密钥。"
                >
                  <SourceState status={source("wechat")} />
                  {!!status?.wechat_candidates?.length &&
                    !config.wechat.data_dir && (
                      <div className="discovered-paths">
                        <p className="hint">发现此 Mac 上的微信账号目录：</p>
                        {status.wechat_candidates.map((path) => (
                          <button
                            className="text-button"
                            key={path}
                            title={path}
                            onClick={() =>
                              updateConfig((previous) => ({
                                ...previous,
                                wechat: { ...previous.wechat, data_dir: path },
                              }))
                            }
                          >
                            选择 {path.split("/").at(-1)}
                          </button>
                        ))}
                      </div>
                    )}
                  <form
                    onSubmit={(event) =>
                      onSubmit(
                        event,
                        "连接微信",
                        async () => {
                          await bridge.command("connect_wechat", {
                            data_dir: config.wechat.data_dir,
                            config_file: config.wechat.config_file || undefined,
                            ...(wechatDataKey
                              ? { data_key: wechatDataKey }
                              : {}),
                            ...(wechatImageKey
                              ? { img_key: wechatImageKey }
                              : {}),
                          });
                          const connected = await bridge.getStatus();
                          if (connected.config?.wechat) {
                            setConfig((previous) => ({
                              ...previous,
                              wechat: normalizeConfig(connected.config).wechat,
                            }));
                          }
                          setWechatDataKey("");
                          setWechatImageKey("");
                        },
                        "微信连接设置已保存。同步状态会自动更新。",
                      )
                    }
                  >
                    <Field label="微信数据目录">
                      <div className="path-input">
                        <input
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          aria-label="微信数据目录"
                          value={config.wechat.data_dir}
                          onChange={(event) =>
                            updateConfig((previous) => ({
                              ...previous,
                              wechat: {
                                ...previous.wechat,
                                data_dir: event.target.value,
                              },
                            }))
                          }
                          placeholder="选择包含微信数据库的目录"
                        />
                        <button
                          className="button"
                          type="button"
                          disabled={!!busy}
                          onClick={() =>
                            void run("选择目录", () => pickPath("directory"))
                          }
                        >
                          选择…
                        </button>
                      </div>
                    </Field>
                    <Field label="已有配置文件（可选）">
                      <div className="path-input">
                        <input
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          aria-label="已有配置文件（可选）"
                          value={config.wechat.config_file}
                          onChange={(event) =>
                            updateConfig((previous) => ({
                              ...previous,
                              wechat: {
                                ...previous.wechat,
                                config_file: event.target.value,
                              },
                            }))
                          }
                          placeholder="选择 Chatlog 配置文件"
                        />
                        <button
                          className="button"
                          type="button"
                          disabled={!!busy}
                          onClick={() =>
                            void run("选择配置", () => pickPath("file"))
                          }
                        >
                          选择…
                        </button>
                      </div>
                    </Field>
                    <details className="credentials-details">
                      <summary>手动填写解密密钥</summary>
                      <Field label="数据库密钥">
                        <input
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          type="password"
                          autoComplete="off"
                          value={wechatDataKey}
                          onChange={(event) =>
                            setWechatDataKey(event.target.value)
                          }
                          placeholder="留空保留已保存的密钥"
                        />
                      </Field>
                      <Field label="图片密钥（可选）">
                        <input
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          type="password"
                          autoComplete="off"
                          value={wechatImageKey}
                          onChange={(event) =>
                            setWechatImageKey(event.target.value)
                          }
                          placeholder="留空保留已保存的密钥"
                        />
                      </Field>
                    </details>
                    <button
                      className="button"
                      type="submit"
                      disabled={
                        !canAct ||
                        (!config.wechat.data_dir && !config.wechat.config_file)
                      }
                    >
                      连接微信
                    </button>
                  </form>
                </Section>
                <Section
                  title="Instagram"
                  description="登录自己的账号，按已有的采集节奏持续更新。"
                >
                  <SourceState status={source("instagram")} />
                  <Toggle
                    label="启用 Instagram"
                    checked={config.sources.instagram.enabled}
                    disabled={!!busy}
                    onChange={(enabled) =>
                      updateConfig((previous) => ({
                        ...previous,
                        sources: {
                          ...previous.sources,
                          instagram: { ...previous.sources.instagram, enabled },
                        },
                      }))
                    }
                  />
                  <Field label="用户名">
                    <input
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                      autoComplete="username"
                      value={config.sources.instagram.username}
                      onChange={(event) =>
                        updateConfig((previous) => ({
                          ...previous,
                          sources: {
                            ...previous.sources,
                            instagram: {
                              ...previous.sources.instagram,
                              username: event.target.value,
                            },
                          },
                        }))
                      }
                    />
                  </Field>
                  <p className="hint">
                    登录会打开独立浏览器窗口；请在其中输入密码并完成验证。
                  </p>
                  <div className="button-row">
                    <button
                      className="button"
                      disabled={!canAct || !config.sources.instagram.username}
                      onClick={() =>
                        void run(
                          "登录 Instagram",
                          async () => {
                            await saveConfig();
                            await bridge.command("source_login", {
                              source: "instagram",
                              username: config.sources.instagram.username,
                            });
                          },
                          "Instagram 登录完成。",
                        )
                      }
                    >
                      登录 Instagram
                    </button>
                    <button
                      className="text-button"
                      disabled={!canAct || !config.sources.instagram.enabled}
                      onClick={() =>
                        void run(
                          "更新 Instagram",
                          async () => {
                            await saveConfig();
                            await bridge.command("collect", {
                              source: "instagram",
                            });
                          },
                          "已请求更新 Instagram。",
                        )
                      }
                    >
                      立即更新
                    </button>
                  </div>
                </Section>
                <Section
                  title="LinkedIn"
                  description="在独立浏览器窗口中完成登录，Monica 会保留登录状态。"
                >
                  <SourceState status={source("linkedin")} />
                  <Toggle
                    label="启用 LinkedIn"
                    checked={config.sources.linkedin.enabled}
                    disabled={!!busy}
                    onChange={(enabled) =>
                      updateConfig((previous) => ({
                        ...previous,
                        sources: { ...previous.sources, linkedin: { enabled } },
                      }))
                    }
                  />
                  <div className="button-row">
                    <button
                      className="button"
                      disabled={!canAct}
                      onClick={() =>
                        void run(
                          "打开 LinkedIn 登录",
                          async () => {
                            await saveConfig();
                            await bridge.command("source_login", {
                              source: "linkedin",
                            });
                          },
                          "LinkedIn 登录完成。",
                        )
                      }
                    >
                      登录 LinkedIn
                    </button>
                    <button
                      className="text-button"
                      disabled={!canAct || !config.sources.linkedin.enabled}
                      onClick={() =>
                        void run(
                          "更新 LinkedIn",
                          async () => {
                            await saveConfig();
                            await bridge.command("collect", {
                              source: "linkedin",
                            });
                          },
                          "已请求更新 LinkedIn。",
                        )
                      }
                    >
                      立即更新
                    </button>
                  </div>
                </Section>
                <Section
                  title="照片里的人"
                  description="使用本机已安装的人脸模型识别，随后在 Monica 中确认人物。公开发行版需自行提供模型。"
                >
                  <SourceState status={source("faces")} />
                  <Toggle
                    label="启用本地人脸识别"
                    checked={config.sources.faces.enabled}
                    disabled={!!busy}
                    onChange={(enabled) =>
                      updateConfig((previous) => ({
                        ...previous,
                        sources: { ...previous.sources, faces: { enabled } },
                      }))
                    }
                  />
                </Section>
              </>
            )}

            {tab === "ai" && (
              <>
                <Section
                  title="模型服务"
                  description="连接模型 API，生成近况摘要、图片识别和定期回顾。"
                >
                  <Toggle
                    label="启用模型任务"
                    description="关闭后仍可查看已有结果。"
                    checked={config.ai.enabled}
                    disabled={!!busy}
                    onChange={(enabled) =>
                      updateConfig((previous) => ({
                        ...previous,
                        ai: { ...previous.ai, enabled },
                      }))
                    }
                  />
                  <Field
                    label="API 地址"
                    hint="填写兼容 OpenAI 的接口地址，通常以 /v1 结尾。"
                  >
                    <input
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                      type="url"
                      placeholder="https://api.example.com/v1"
                      value={config.ai.base_url}
                      onChange={(event) =>
                        updateConfig((previous) => ({
                          ...previous,
                          ai: { ...previous.ai, base_url: event.target.value },
                        }))
                      }
                    />
                  </Field>
                  <Field
                    label="API Key"
                    hint={
                      status?.config?.ai?.api_key_set
                        ? "已保存密钥。留空会继续使用现有密钥。"
                        : "密钥保存在仅当前 Mac 用户可读取的本地配置文件中。"
                    }
                  >
                    <input
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                      type="password"
                      autoComplete="off"
                      value={apiKey}
                      onChange={(event) => {
                        setApiKey(event.target.value);
                        dirtyRef.current = true;
                        setDirty(true);
                      }}
                      placeholder={
                        status?.config?.ai?.api_key_set
                          ? "已保存"
                          : "填写 API Key"
                      }
                    />
                  </Field>
                  <div className="field-grid">
                    <Field label="文本模型">
                      <input
                        autoCapitalize="none"
                        autoCorrect="off"
                        spellCheck={false}
                        value={config.ai.batch_model}
                        placeholder="模型名称"
                        onChange={(event) =>
                          updateConfig((previous) => ({
                            ...previous,
                            ai: {
                              ...previous.ai,
                              batch_model: event.target.value,
                            },
                          }))
                        }
                      />
                    </Field>
                    <Field label="图片模型">
                      <input
                        autoCapitalize="none"
                        autoCorrect="off"
                        spellCheck={false}
                        value={config.ai.vision_model}
                        placeholder="支持图片输入的模型名称"
                        onChange={(event) =>
                          updateConfig((previous) => ({
                            ...previous,
                            ai: {
                              ...previous.ai,
                              vision_model: event.target.value,
                            },
                          }))
                        }
                      />
                    </Field>
                  </div>
                  <button
                    className="button"
                    disabled={!canAct || !config.ai.base_url}
                    onClick={() =>
                      void run(
                        "测试模型连接",
                        async () => {
                          await saveConfig();
                          await bridge.command("test_ai");
                        },
                        "模型服务连接成功。",
                      )
                    }
                  >
                    保存并测试连接
                  </button>
                </Section>
                <p className="footnote">
                  模型任务需要联网，并使用你配置的服务。已有资料和本地浏览不受模型服务连接状态影响。
                </p>
              </>
            )}

            {tab === "general" && (
              <>
                <Section title="启动与后台运行">
                  <Toggle
                    label="登录 Mac 后自动运行"
                    description="登录后在菜单栏启动 Monica。"
                    checked={config.autostart}
                    disabled={!canAct}
                    onChange={(enabled) =>
                      void run(
                        "更新启动设置",
                        async () => {
                          await bridge.setAutostart(enabled);
                          setConfig((previous) => ({
                            ...previous,
                            autostart: enabled,
                          }));
                        },
                        enabled
                          ? "已开启登录后自动运行。"
                          : "已关闭登录后自动运行。",
                      )
                    }
                  />
                  <div className="note">
                    Mac
                    睡眠时任务会暂停；唤醒后会从已保存的进度继续。关闭设置窗口不会停止后台运行。
                  </div>
                </Section>
                <Section
                  title="本地数据"
                  description="资料和媒体保存在你的用户目录中，升级应用时会保留。"
                >
                  <div className="directory-description">
                    <span>数据目录</span>
                    <code>~/Library/Application Support/Monica</code>
                  </div>
                  <button
                    className="button"
                    disabled={!!busy}
                    onClick={() => void run("打开日志", bridge.openLogs)}
                  >
                    打开日志文件夹
                  </button>
                </Section>
                <Section
                  title="退出 Monica"
                  description="退出会停止同步和本地服务。下次打开时继续处理。"
                >
                  <button
                    className="button"
                    disabled={!!busy || state === "stopping"}
                    onClick={() =>
                      void run("等待任务保存并退出", bridge.quitApp)
                    }
                  >
                    退出 Monica
                  </button>
                </Section>
              </>
            )}
          </fieldset>
        </main>
        {(tab === "sources" || tab === "ai") && (
          <footer className="save-bar">
            <span>{dirty ? "有尚未保存的更改" : "设置已保存"}</span>
            <button
              className="button primary"
              disabled={!canAct || !dirty}
              onClick={() => void run("保存设置", saveConfig, "设置已保存。")}
            >
              保存设置
            </button>
          </footer>
        )}
      </div>
    </div>
  );
}
