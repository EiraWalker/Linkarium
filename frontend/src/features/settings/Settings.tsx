import { useEffect, useState } from "preact/hooks";
import { errorMessage, request } from "../../api/client";
import type { CaptureNetworkSettings, Profile, RuntimeStatus } from "../../api/types";
import { useApp } from "../../app/context";
import { useResource } from "../../app/hooks";
import {
  Button,
  ErrorNotice,
  Field,
  Icon,
  Time,
} from "../../components/UI";
import s from "./Settings.module.css";
import { DataActions } from "./DataActions";
import { PasskeySettings } from "./Passkeys";
import { LocalAISettings } from "./LocalAI";

export function SettingsPage() {
  const app = useApp();
  const profiles = useResource<Profile[]>("/ai-profiles", app.refresh);
  const runtime = useResource<RuntimeStatus>(
    "/runtime/status",
    app.refresh,
  );
  const [days, setDays] = useState("7");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (app.settings)
      setDays(String(app.settings.default_refresh_seconds / 86400));
  }, [app.settings?.revision]);
  async function saveDefault(e: Event) {
    e.preventDefault();
    if (!app.settings) return;
    setBusy(true);
    setError("");
    try {
      await request("/settings", {
        method: "PATCH",
        body: {
          revision: app.settings.revision,
          default_refresh_seconds: Math.round(Number(days) * 86400),
        },
      });
      app.changed();
      app.notify("默认刷新间隔已保存。");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main class={s.page}>
      <h1>设置</h1>
      <p class="muted">设置保存在服务端，你的电脑和手机共享同一个收藏库。</p>
      <PasskeySettings connection={runtime.data?.connection} connectionReady={!!runtime.data} />
      <section class={s.section}>
        <h2>
          <Icon name="clock" />
          刷新设置
        </h2>
        <ErrorNotice message={error} />
        <form onSubmit={saveDefault}>
          <Field
            label="默认刷新间隔（天）"
            hint="网站可单独设置间隔；没有独立配置时使用此值。"
          >
            <input
              type="number"
              min="0.0007"
              step="any"
              required
              value={days}
              onInput={(e) => setDays(e.currentTarget.value)}
            />
          </Field>
          <Button primary type="submit" disabled={busy || !app.settings}>
            保存刷新设置
          </Button>
        </form>
      </section>
        <CaptureNetworkForm />
      <section class={s.section}>
          <h2><Icon name="folder" />本机 AI 分类</h2>
          <LocalAISettings />
          {!!profiles.data?.length && <p class="muted">保留 {profiles.data.length} 份历史服务器配置供兼容使用；分类不再读取其凭据。</p>}
      </section>
      <section class={s.section}>
        <h2>
          <Icon name="settings" />
          运行状态
        </h2>
        <ErrorNotice message={runtime.error} retry={runtime.reload} />
        {runtime.data ? (
          <dl class={s.runtime}>
            {runtimeRows(runtime.data).map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p class="muted">正在读取状态…</p>
        )}
          <Button onClick={runtime.reload} icon="refresh">
            重新检查
          </Button>
        </section>
        <DataActions libraryEpoch={typeof runtime.data?.library_epoch === "number" ? runtime.data.library_epoch : undefined} />
      </main>
  );
}
function CaptureNetworkForm() {
  const app = useApp();
  const network = useResource<CaptureNetworkSettings>("/capture-network");
  const [mode, setMode] = useState<CaptureNetworkSettings["mode"]>("direct");
  const [proxyUrl, setProxyUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (network.data) {
      setMode(network.data.mode);
      setProxyUrl(network.data.proxy_url);
    }
  }, [network.data]);

  async function save(e: Event) {
    e.preventDefault();
    if (busy || !network.data) return;
    setBusy(true);
    setError("");
    try {
      const response = await request<CaptureNetworkSettings>("/capture-network", {
        method: "PATCH",
        body: { mode, proxy_url: proxyUrl.trim() },
      });
      network.setResponse(response);
      app.notify("网页缓存代理已保存，将用于后续缓存任务。");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section class={s.section}>
      <h2><Icon name="settings" />网页缓存代理</h2>
      <p class="muted">为后续网页缓存任务设置代理。选择 Clash Verge 后，会自动使用后端所在电脑的本机代理。</p>
      <ErrorNotice message={network.error} retry={network.reload} />
      <ErrorNotice message={error} />
      {network.data ? (
        <form onSubmit={save} aria-busy={busy}>
          <Field label="代理方式">
            <select
              value={mode}
              disabled={busy}
              onChange={(e) => {
                const value = e.currentTarget.value;
                if (value === "direct" || value === "clash" || value === "manual") {
                  setMode(value);
                  setError("");
                }
              }}
            >
              <option value="direct">不使用代理</option>
              <option value="clash">Clash Verge（本机）</option>
              <option value="manual">手动 HTTP 代理地址</option>
            </select>
          </Field>
          {mode === "manual" && (
            <Field label="HTTP 代理地址" hint="使用本机 HTTP 代理地址，例如 http://127.0.0.1:7897。">
              <input
                type="url"
                required
                disabled={busy}
                value={proxyUrl}
                placeholder="http://127.0.0.1:7897"
                autoComplete="off"
                spellcheck={false}
                onInput={(e) => setProxyUrl(e.currentTarget.value)}
              />
            </Field>
          )}
          {mode === "clash" && (
            <p class="muted" role="status">
              {network.data.detected_proxy_url
                ? `检测到本机代理：${network.data.detected_proxy_url}`
                : "尚未检测到 Clash Verge 代理，请先在后端所在电脑上启动 Clash Verge。"}
            </p>
          )}
          <p class="muted" role="status">
            {network.data.active_proxy_url
              ? `已保存的代理：${network.data.active_proxy_url}`
              : network.data.mode === "direct"
                ? "当前设置：不使用代理。"
                : "已保存的代理当前不可用，请检查本机代理配置。"}
          </p>
          <Button primary type="submit" disabled={busy}>
            {busy ? "正在保存…" : "保存代理设置"}
          </Button>
        </form>
      ) : network.loading ? <p class="muted" role="status">正在读取代理设置…</p> : null}
    </section>
  );
}

function runtimeRows(status: Record<string, unknown>) {
  const available = (key: string) => (status[key] === true ? "可用" : "未就绪");
  const bytes = (value: unknown) =>
    typeof value === "number"
      ? `${(value / 1024 ** 3).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} GiB`
      : "—";
  return [
    ["收藏库状态", status.maintenance_mode ? "维护中" : "正常"],
    ["后台任务", available("worker_alive")],
    [
      "上次任务心跳",
      <Time
        value={
          typeof status.worker_heartbeat === "string"
            ? status.worker_heartbeat
            : null
        }
      />,
    ],
    ["网页采集", available("capture_available")],
    ["可用磁盘空间", bytes(status.free_disk_bytes ?? status.disk_free_bytes)],
    ...(typeof status.library_bytes === "number"
      ? [["收藏库大小", bytes(status.library_bytes)]]
      : []),
  ] as const;
}
