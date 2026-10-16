import { useEffect, useState } from "preact/hooks";
import { errorMessage, request } from "../../api/client";
import type { CaptureNetworkSettings, Job, Profile } from "../../api/types";
import { useApp } from "../../app/context";
import { useResource } from "../../app/hooks";
import {
  Button,
  ErrorNotice,
  Field,
  Icon,
  stateLabel,
  terminalState,
  Time,
  errorCodeLabel,
} from "../../components/UI";
import s from "./Settings.module.css";
import { DataActions } from "./DataActions";
import { PasskeySettings } from "./Passkeys";

export function SettingsPage() {
  const app = useApp();
  const profiles = useResource<Profile[]>("/ai-profiles", app.refresh);
  const runtime = useResource<Record<string, unknown>>(
    "/runtime/status",
    app.refresh,
  );
  const [days, setDays] = useState("7");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string>("");
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    if (app.settings)
      setDays(String(app.settings.default_refresh_seconds / 86400));
  }, [app.settings?.revision]);
  useEffect(() => {
    if (profiles.data?.length && !selected && !creating)
      setSelected(profiles.data[0].id);
  }, [profiles.data]);
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
  const profile = profiles.data?.find((p) => p.id === selected);
  return (
    <main class={s.page}>
      <h1>设置</h1>
      <p class="muted">设置保存在服务端，你的电脑和手机共享同一个收藏库。</p>
      <PasskeySettings />
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
        <div class={s.heading}>
          <h2>
            <Icon name="folder" />
            AI 自动分类
          </h2>
          <Button
            icon="plus"
            onClick={() => {
              setCreating(true);
              setSelected("");
            }}
          >
            新建配置
          </Button>
        </div>
        <p class="muted">
          软件会发送网址、标题、简介与有限正文片段。分类建议需人工确认，才会建立目录和移动网站。
        </p>
        <ErrorNotice message={profiles.error} retry={profiles.reload} />
        {!!profiles.data?.length && (
          <Field label="选择配置">
            <select
              value={selected}
              onChange={(e) => {
                setSelected(e.currentTarget.value);
                setCreating(false);
              }}
            >
              <option value="">新建配置</option>
              {profiles.data.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
        )}
        <ProfileForm
          key={profile?.id ?? "new"}
          profile={profile}
          onSaved={(id) => {
            setSelected(id);
            setCreating(false);
            app.changed();
          }}
        />
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
function ProfileForm({
  profile,
  onSaved,
}: {
  profile?: Profile;
  onSaved: (id: string) => void;
}) {
  const app = useApp();
  const [name, setName] = useState(profile?.name ?? "默认 AI");
  const [base, setBase] = useState(
    profile?.base_url ?? profile?.endpoint ?? "",
  );
  const [model, setModel] = useState(profile?.model ?? "");
  const [key, setKey] = useState("");
  const [enabled, setEnabled] = useState(profile?.enabled ?? true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [testId, setTestId] = useState<string | null>(null);
  const [testMessage, setTestMessage] = useState("");
  const test = useResource<Job>(
    testId ? `/jobs/${testId}` : null,
    0,
    testId ? 2000 : 0,
  );
  useEffect(() => {
    if (test.data && terminalState(test.data.state)) {
      setTestMessage(
        test.data.state === "succeeded"
          ? "连接验证成功，配置已可用于分类。"
          : `验证失败：${test.data.error_code ? errorCodeLabel(test.data.error_code) : (test.data.error_message ?? stateLabel(test.data.state))}`,
      );
      setTestId(null);
      app.changed();
    }
  }, [test.data?.state]);
  async function save(e: Event) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const { data } = await request<Profile>(
        profile ? `/ai-profiles/${profile.id}` : "/ai-profiles",
        {
          method: profile ? "PATCH" : "POST",
          body: {
            name,
            base_url: base,
            model,
            protocol: "chat-completions",
            enabled,
            ...(profile ? { revision: profile.revision } : {}),
            ...(key ? { api_key: key } : {}),
          },
        },
      );
      setKey("");
      onSaved(data.id);
      app.notify("AI 配置已保存，请测试连接后使用。");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function testConnection() {
    if (!profile) return;
    setBusy(true);
    setError("");
    setTestMessage("");
    try {
      const { data } = await request<{
        job_id?: string;
        group_id?: string;
        message?: string;
        success?: boolean;
        state?: string;
      }>(`/ai-profiles/${profile.id}/test`, { method: "POST", body: {} });
      if (data.job_id) setTestId(data.job_id);
      else {
        setTestMessage(
          data.message ??
            (data.success || data.state === "succeeded"
              ? "连接验证成功。"
              : "验证请求已提交。"),
        );
        app.changed();
      }
      if (data.group_id) app.trackGroup(data.group_id);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save}>
      <ErrorNotice message={error || test.error} />
      <div class={s.formGrid}>
        <Field label="配置名称">
          <input
            required
            value={name}
            maxLength={100}
            onInput={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <Field label="模型名称">
          <input
            required
            value={model}
            placeholder="填写服务商提供的模型名称"
            onInput={(e) => setModel(e.currentTarget.value)}
          />
        </Field>
      </div>
      <Field
        label="API 地址"
        hint="支持 OpenAI Chat Completions 兼容协议，地址包含服务商要求的 /v1 等路径。"
      >
        <input
          type="url"
          required
          value={base}
          placeholder="https://api.example.com/v1"
          onInput={(e) => setBase(e.currentTarget.value)}
        />
      </Field>
      <Field
        label="API 密钥"
        hint={
          profile?.key_configured
            ? "密钥已配置；留空保留。密钥不会进入收藏库备份。"
            : "密钥只由服务端保存，不会在浏览器持久保存。"
        }
      >
        <input
          type="password"
          autoComplete="off"
          value={key}
          onInput={(e) => setKey(e.currentTarget.value)}
          placeholder={profile?.key_configured ? "已配置密钥" : "输入 API 密钥"}
        />
      </Field>
      <label class="inline">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.currentTarget.checked)}
        />
        启用该配置
      </label>
      {profile && (
        <p
          class={
            profile.key_verified && profile.binding_matches
              ? "status-good"
              : "muted"
          }
        >
          {profile.key_verified && profile.binding_matches
            ? "已验证 · 可调用"
            : "尚未验证 · 保存后请测试连接"}
        </p>
      )}
      {testMessage && <p role="status">{testMessage}</p>}
      {testId && (
        <p class="muted" role="status">
          正在验证服务连接与分类输出…
        </p>
      )}
      <div class="inline">
        <Button primary type="submit" disabled={busy}>
          保存配置
        </Button>
        <Button
          onClick={() => void testConnection()}
          disabled={busy || !profile || !!testId}
        >
          测试连接
        </Button>
      </div>
      {profile && (
        <div class={s.activeProfile}>
          <Button
            onClick={() => {
              if (!app.settings) return;
              void request("/settings", {
                method: "PATCH",
                body: {
                  revision: app.settings.revision,
                  active_ai_profile_id: profile.id,
                },
              })
                .then(() => {
                  app.changed();
                  app.notify("已设为自动分类使用的配置。");
                })
                .catch((e) => setError(errorMessage(e)));
            }}
            disabled={app.settings?.active_ai_profile_id === profile.id}
          >
            {app.settings?.active_ai_profile_id === profile.id
              ? "当前自动分类配置"
              : "设为自动分类配置"}
          </Button>
        </div>
      )}
    </form>
  );
}
