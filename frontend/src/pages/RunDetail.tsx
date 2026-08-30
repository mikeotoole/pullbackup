// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { useEffect, useRef, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { StatusPill } from "../components/StatusPill";

export function RunDetail() {
  const { id } = useParams();
  const runId = Number(id);
  const { data: run } = useQuery({ queryKey: ["run", runId], queryFn: () => api.getRun(runId), refetchInterval: 2000 });
  const [log, setLog] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!runId) return;
    const es = new EventSource(`/api/runs/${runId}/log/stream`);
    es.addEventListener("log", e => setLog(l => l + (e as MessageEvent).data + "\n"));
    es.addEventListener("end", () => es.close());
    return () => es.close();
  }, [runId]);

  useEffect(() => { endRef.current?.scrollIntoView(); }, [log]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <Link to="/tasks" className="text-muted hover:text-white">← Tasks</Link>
        <h1 className="text-xl font-semibold">Run #{runId}</h1>
        <StatusPill state={run?.state} />
      </div>
      {run && (
        <div className="bg-panel border border-border rounded p-4 text-sm grid grid-cols-2 gap-3">
          <div><span className="text-muted">Started:</span> {run.started_at}</div>
          <div><span className="text-muted">Finished:</span> {run.finished_at ?? "—"}</div>
          <div><span className="text-muted">Exit code:</span> {run.exit_code ?? "—"}</div>
          <div><span className="text-muted">Files:</span> {run.files_transferred ?? "—"}</div>
          <div><span className="text-muted">Bytes:</span> {run.bytes_transferred?.toLocaleString() ?? "—"}</div>
        </div>
      )}
      <pre className="bg-bg border border-border rounded p-3 text-xs overflow-x-auto h-[60vh] overflow-y-auto whitespace-pre-wrap">{log || "(waiting for log…)"}<div ref={endRef} /></pre>
    </div>
  );
}
