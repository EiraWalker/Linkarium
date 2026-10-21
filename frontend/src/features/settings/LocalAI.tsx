import { useEffect, useState } from "preact/hooks";
import { executorStatus, openExecutorSettings, type ExecutorStatus } from "../../api/localExecutor";
import { errorMessage } from "../../api/client";
import { Button } from "../../components/UI";

export function LocalAISettings() {
  const [status, setStatus] = useState<ExecutorStatus>();
  const [message, setMessage] = useState("正在检查本浏览器的执行端…");
  async function refresh() {
    try {
      const value = await executorStatus();
      setStatus(value);
      setMessage(value.protocol !== 1 ? "请升级 Firefox 插件" : value.verified ? "本设备 API 已验证" : "请在插件中配置并验证本机 API");
    } catch (error) {setMessage(errorMessage(error));}
  }
  useEffect(() => {void refresh();}, []);
  return <>
    <p>所有 AI 分类均由发起设备的 Firefox 插件调用 OpenAI 兼容 API。云端准备 prompt 并校验结果，密钥仅保存在本设备。</p>
    <p role="status">{message}</p>
    {status && <p class="muted">发起设备：{status.device_id} · {status.state?.message || "等待分类任务"}</p>}
    <Button onClick={() => {void openExecutorSettings().catch(error => setMessage(errorMessage(error)));}}>打开本设备 AI 设置</Button>{" "}
    <Button onClick={() => {void refresh();}}>重新检查</Button>
    <p class="muted">Firefox 退出后分类暂停，重新打开后由原设备恢复。网页与导入的结果仍需人工校对；插件新建收藏沿用自动归档。</p>
  </>;
}
