import { useEffect, useState } from "preact/hooks";
import type { Job } from "../api/types";
import { executorStatus } from "../api/localExecutor";
import { request, errorMessage } from "../api/client";
import { Button, stateLabel, errorCodeLabel, terminalState } from "./UI";

export function LocalTaskState({job}: {job: Job}) {
  return <><span>{stateLabel(terminalState(job.state) && job.state !== "succeeded" ? job.state : job.execution_state)}</span>
    <small class="muted" style={{display: "block"}}>发起设备：{job.execution_device_id?.slice(0, 8)}{job.error_code ? " · " + errorCodeLabel(job.error_code) : ""}</small></>;
}

export function LocalTaskActions({job, changed}: {job: Job; changed: () => void}) {
  const [owner, setOwner] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {void executorStatus().then(status => setOwner(status.device_id === job.execution_device_id)).catch(() => {});}, [job.execution_device_id]);
  async function act(action: string) {
    try {await request(`/jobs/${job.id}/${action}`, {method: "POST", body: {}}); setMessage(""); changed();}
    catch (error) {setMessage(errorMessage(error));}
  }
  return <>
    {owner && (["paused", "storage_blocked"].includes(job.execution_state || "") || ["failed", "retry_wait"].includes(job.state)) && <Button onClick={() => {void act("retry");}}>在本设备恢复</Button>}
    {!terminalState(job.state) && <Button onClick={() => {void act("cancel");}}>取消</Button>}
    {!owner && !terminalState(job.state) && <small class="muted">由发起设备继续</small>}
    {message && <small role="alert">{message}</small>}
  </>;
}
