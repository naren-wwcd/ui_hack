import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { API_BASE, PORTAL_URL, api, screenshotUrl } from "./api.js";
import { useSSE } from "./hooks/useSSE.js";

const FAULTS = [
  ["bad_config", "Bad config", "Wrong database host"],
  ["pool_leak", "Pool leak", "Connections accumulate"],
  ["disk_full", "Disk full", "Log growth exhausts disk"],
  ["payment_down", "Payment down", "Gateway becomes unavailable"],
  ["db_hung", "DB hung", "Database becomes unresponsive"],
  ["compound", "Compound", "Bad config + full disk"],
];

const PHASES = {
  tool_call: ["⌕", "Investigate"],
  update_hypotheses: ["◇", "Hypothesize"],
  propose_fix: ["◎", "Decide"],
  guard_decision: ["⬡", "Guard"],
  act: ["⚡", "Act"],
  verify: ["✓", "Verify"],
  escalate: ["!", "Escalate"],
  conclude: ["✓", "Conclude"],
};

function parseJson(value, fallback = {}) {
  if (value && typeof value === "object") return value;
  try {
    return JSON.parse(value || "{}");
  } catch {
    return fallback;
  }
}

function timeLabel() {
  return new Date().toLocaleTimeString([], { hour12: false });
}

function formatDuration(seconds) {
  const safe = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(safe / 60);
  return `${String(minutes).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function StatusPill({ value }) {
  const normalized = String(value || "unknown").toLowerCase();
  const tone = ["healthy", "running", "ok", "resolved", "done", "approved"].includes(normalized)
    ? "good"
    : ["degraded", "pending", "investigating", "awaiting_approval"].includes(normalized)
      ? "warn"
      : ["down", "unresponsive", "failed", "rejected", "escalated"].includes(normalized)
        ? "bad"
        : "neutral";
  return <span className={`pill ${tone}`}>{String(value || "unknown").replaceAll("_", " ")}</span>;
}

function Header({ llmMode, connection, activeTab, setActiveTab }) {
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark">AH</div>
        <div>
          <div className="brand-title">AUTOHEAL <span>OPS</span></div>
          <div className="brand-caption">AI decides · Automation executes · Humans govern</div>
        </div>
      </div>
      <nav className="topnav">
        {[
          ["dashboard", "Overview"],
          ["incident", "Incident"],
          ["audit", "Audit log"],
        ].map(([id, label]) => (
          <button
            key={id}
            className={`nav-button ${activeTab === id ? "selected" : ""}`}
            onClick={() => setActiveTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className="badges">
        <span className="sandbox-badge"><i /> SANDBOX</span>
        <span className={`mode-badge ${llmMode === "LIVE" ? "live" : "replay"}`}>
          LLM: {llmMode || "REPLAY"}
        </span>
        <span className={`connection ${connection === "connected" ? "online" : ""}`}>
          <i /> {connection}
        </span>
      </div>
    </header>
  );
}

function HealthTile({ name, value, subtitle }) {
  return (
    <div className="health-tile">
      <div className="tile-heading"><span className="service-dot" />{name}</div>
      <StatusPill value={value} />
      {subtitle && <div className="tile-subtitle">{subtitle}</div>}
    </div>
  );
}

function MetricChart({ title, unit, dataKey, data, color, suffix = "" }) {
  return (
    <section className="panel chart-panel">
      <div className="panel-heading">
        <h3>{title}</h3>
        <span>{unit}</span>
      </div>
      {data.length < 2 ? (
        <div className="chart-empty">Waiting for live sandbox metrics…</div>
      ) : (
        <div className="chart">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
              <defs>
                <linearGradient id={`fill-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={color} stopOpacity={0.01} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#243247" strokeDasharray="3 5" vertical={false} />
              <XAxis dataKey="time" tick={{ fill: "#728197", fontSize: 10 }} />
              <YAxis tick={{ fill: "#728197", fontSize: 10 }} />
              <Tooltip
                contentStyle={{ background: "#111a28", border: "1px solid #263448", borderRadius: 8 }}
                labelStyle={{ color: "#dce7f4" }}
                formatter={(value) => [`${Number(value).toFixed(2)}${suffix}`, title]}
              />
              <Area
                type="monotone"
                dataKey={dataKey}
                stroke={color}
                strokeWidth={2}
                fill={`url(#fill-${dataKey})`}
                isAnimationActive
                animationDuration={500}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}

function Dashboard({ health, chartData, mttr, onFault, onRandom, onReset, busy }) {
  const services = health?.services || {};
  const overall = health?.overall || "DEGRADED";
  return (
    <>
      <section className={`status-banner ${overall.toLowerCase()}`}>
        <div>
          <div className="eyebrow">SANDBOX SERVICE STATUS</div>
          <h1>{overall}</h1>
          <p>{overall === "HEALTHY" ? "Checkout is responding within the recovery thresholds." : "The sandbox is being observed and investigated."}</p>
        </div>
        <div className="banner-metric">
          <span>INCIDENT MTTR</span>
          <strong>{formatDuration(mttr)}</strong>
        </div>
      </section>

      <section className="health-grid">
        <HealthTile name="APP" value={services.app} subtitle="ShopNode :8001" />
        <HealthTile name="DATABASE" value={services.db} />
        <HealthTile name="WORKER" value={services.worker} />
        <HealthTile name="DISK" value={services.disk} subtitle={`${Number(health?.disk_used_pct || 0).toFixed(0)}% used`} />
        <HealthTile name="PAYMENT" value={services.payment} />
      </section>

      <section className="charts-grid">
        <MetricChart title="Checkout error rate" unit="rolling window · %" dataKey="error_rate" data={chartData} color="#ff6278" suffix="%" />
        <MetricChart title="Checkout p95 latency" unit="rolling window · ms" dataKey="p95_ms" data={chartData} color="#4dd9e8" suffix=" ms" />
      </section>

      <section className="panel break-panel">
        <div className="panel-heading">
          <div>
            <div className="eyebrow">CONTROLLED FAILURE INJECTION</div>
            <h2>Break the sandbox</h2>
          </div>
          <button className="button danger-outline" disabled={busy} onClick={onReset}>Reset sandbox</button>
        </div>
        <div className="fault-grid">
          {FAULTS.map(([name, title, detail]) => (
            <button className="fault-button" key={name} disabled={busy} onClick={() => onFault(name)}>
              <span className="fault-symbol">+</span>
              <span><b>{title}</b><small>{detail}</small></span>
            </button>
          ))}
          <button className="fault-button random" disabled={busy} onClick={onRandom}>
            <span className="fault-symbol">?</span>
            <span><b>Random fault</b><small>Cause hidden from the agent</small></span>
          </button>
        </div>
        <p className="microcopy">Fault injection and remediation affect only this local SANDBOX.</p>
      </section>
    </>
  );
}

function HypothesisCard({ hypothesis }) {
  const confidence = Math.max(0, Math.min(1, Number(hypothesis.confidence) || 0));
  const status = String(hypothesis.status || "open").toLowerCase();
  return (
    <article className="hypothesis-card">
      <div className="hypothesis-top">
        <strong>{hypothesis.cause}</strong>
        <StatusPill value={status} />
      </div>
      <div className="confidence-row">
        <span>Confidence</span><b>{Math.round(confidence * 100)}%</b>
      </div>
      <div className="confidence-track"><div style={{ width: `${confidence * 100}%` }} /></div>
      {parseJson(hypothesis.evidence_json, {}).evidence_for?.length > 0 && (
        <p className="hypothesis-evidence">For: {parseJson(hypothesis.evidence_json).evidence_for.join(" · ")}</p>
      )}
    </article>
  );
}

function stepInfo(step) {
  const payload = parseJson(step.payload_json);
  const action = payload.action || payload;
  const key = step.type === "tool_call" ? "tool_call"
    : step.type === "update_hypotheses" ? "update_hypotheses"
      : step.type === "propose_fix" ? "propose_fix"
        : step.type === "verify" ? "verify"
          : step.type === "act" ? "act"
            : step.type === "escalate" ? "escalate"
              : step.type === "conclude" ? "conclude"
                : step.type;
  const info = PHASES[key] || ["·", step.type || "Step"];
  return { payload, action, icon: info[0], label: info[1] };
}

function Timeline({ steps }) {
  if (!steps?.length) return <div className="empty">No agent steps yet. The watcher may still be opening the incident.</div>;
  return (
    <div className="timeline">
      {steps.map((step) => {
        const { payload, action, icon, label } = stepInfo(step);
        const result = payload.result || payload.verification;
        const quoteLines = result?.lines || result?.detail?.lines || [];
        return (
          <article className="timeline-item" key={step.id || `${step.idx}-${step.type}`}>
            <div className="timeline-icon">{icon}</div>
            <div className="timeline-content">
              <div className="timeline-heading">
                <strong>{label}</strong><span>#{step.idx}</span>
                <time>{step.ts ? new Date(step.ts).toLocaleTimeString() : ""}</time>
              </div>
              <p>{step.thought || action.summary || action.reason || action.fix_name || ""}</p>
              {action.tool && <div className="tool-chip">TOOL · {action.tool}</div>}
              {action.fix_name && <div className="tool-chip">FIX · {action.fix_name}</div>}
              {quoteLines.length > 0 && (
                <blockquote>{quoteLines.map((line, index) => <code key={index}>“{line}”</code>)}</blockquote>
              )}
              {payload.result?.detail && typeof payload.result.detail === "string" && (
                <div className="result-line">{payload.result.detail}</div>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ApprovalModal({ approval, action, hypothesis, onApprove, onReject, busy }) {
  if (!approval) return null;
  const actionPayload = action || {};
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="approval-modal" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <div className="modal-top"><span className="modal-symbol">!</span><span className="pill warn">HUMAN REVIEW</span></div>
        <h2 id="approval-title">Approval required</h2>
        <p className="modal-diagnosis">{hypothesis?.cause || "Review the proposed sandbox action before it runs."}</p>
        <dl className="approval-details">
          <div><dt>Proposed action</dt><dd>{actionPayload.fix || "Remediation action"}</dd></div>
          <div><dt>Risk</dt><dd>{actionPayload.risk || "Review catalog entry"}</dd></div>
          <div><dt>Blast radius</dt><dd>{actionPayload.blast_radius || "Sandbox services"}</dd></div>
          <div><dt>Reason</dt><dd>{actionPayload.reason || "Guard requires human approval."}</dd></div>
          <div><dt>Rollback plan</dt><dd>{actionPayload.rollback_plan || "Escalate if sandbox verification fails."}</dd></div>
        </dl>
        <div className="modal-actions">
          <button className="button" disabled={busy} onClick={onApprove}>Approve sandbox action</button>
          <button className="button danger-outline" disabled={busy} onClick={onReject}>Reject</button>
        </div>
      </section>
    </div>
  );
}

function RpaPanel({ tasks }) {
  const latest = tasks?.[0];
  const path = latest?.screenshot_path;
  return (
    <section className="panel">
      <div className="panel-heading">
        <div><div className="eyebrow">LEGACY PORTAL AUTOMATION</div><h3>RPA task queue</h3></div>
        <a className="text-link" href={PORTAL_URL} target="_blank" rel="noreferrer">Open ITSM portal ↗</a>
      </div>
      {latest && path ? (
        <div className="screenshot-preview">
          <a href={screenshotUrl(path)} target="_blank" rel="noreferrer">
            <img src={screenshotUrl(path)} alt="Latest RPA portal screenshot" />
          </a>
          <small>{path}</small>
        </div>
      ) : <div className="empty compact">Screenshots appear after the portal bot completes a task.</div>}
      {!tasks?.length ? <div className="empty compact">No portal tasks yet.</div> : (
        <div className="task-list">
          {tasks.map((task) => (
            <div className="task-row" key={task.id}>
              <div><b>{task.kind.replaceAll("_", " ")}</b><small>Task #{task.id}</small></div>
              <StatusPill value={task.status} />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function EscalationCard({ pages }) {
  const latest = pages?.[0];
  if (!latest) {
    return <section className="panel"><h3>On-call escalation</h3><div className="empty compact">No page has been sent for this incident.</div></section>;
  }
  const body = parseJson(latest.body_json, {});
  return (
    <section className="panel escalation-panel">
      <div className="panel-heading"><div><div className="eyebrow">MOCK ON-CALL PAGE</div><h3>Escalation sent</h3></div><StatusPill value="escalated" /></div>
      <p className="diagnosis-text">{body.diagnosis || body.diagnosis_summary || "Diagnosis attached to page."}</p>
      <div className="page-reason">{body.reason}</div>
      <h4>Recommended next step</h4>
      <p>{body.recommended_next_step || "Review the evidence and choose an approved sandbox action."}</p>
      <h4>Evidence sent</h4>
      <ul>{(body.evidence || []).map((item, index) => <li key={index}>{String(item)}</li>)}</ul>
    </section>
  );
}

function AuditLog({ incident }) {
  const records = useMemo(() => {
    if (!incident) return [];
    const events = [
      ...(incident.steps || []).map((item) => ({ ...item, kind: "step", at: item.ts })),
      ...(incident.actions || []).map((item) => ({ ...item, kind: "action", at: item.ts })),
      ...(incident.approvals || []).map((item) => ({ ...item, kind: "approval", at: item.ts })),
    ];
    return events.sort((a, b) => String(a.at || "").localeCompare(String(b.at || "")));
  }, [incident]);

  if (!records.length) return <div className="empty">No audit records for this incident yet.</div>;
  return (
    <div className="audit-list">
      {records.map((record, index) => (
        <div className="audit-row" key={`${record.kind}-${record.id || index}`}>
          <time>{record.at ? new Date(record.at).toLocaleString() : "—"}</time>
          <span className="audit-type">{record.kind}</span>
          <span>
            {record.kind === "step"
              ? `${record.type}: ${record.thought || ""}`
              : record.kind === "action"
                ? `${record.fix} · ${record.decision} · ${record.result || record.reason}`
                : `Approval ${record.status} · ${record.decided_by || "pending"}`}
          </span>
        </div>
      ))}
    </div>
  );
}

function IncidentView({ incident, onRefresh }) {
  if (!incident?.incident) {
    return (
      <section className="panel incident-empty">
        <div className="empty-symbol">⌕</div>
        <h2>No incident selected</h2>
        <p>Inject a sandbox fault or wait for the metric watcher to open an incident automatically.</p>
        <button className="button" onClick={onRefresh}>Refresh incident list</button>
      </section>
    );
  }

  const item = incident.incident;
  const pending = incident.approvals?.find((approval) => approval.status === "pending");
  const pendingAction = pending ? incident.actions?.find((entry) => entry.id === pending.action_id) : null;
  const hypothesis = pendingAction
    ? incident.hypotheses?.find((entry) => String(entry.id) === String(pendingAction.target_hypothesis))
    : null;

  return (
    <div className="incident-layout">
      <main className="incident-main">
        <section className="panel incident-summary">
          <div>
            <div className="eyebrow">INCIDENT #{item.id} · SANDBOX</div>
            <h1>{item.title}</h1>
            <p>Opened {item.opened_at ? new Date(item.opened_at).toLocaleString() : "—"}</p>
          </div>
          <StatusPill value={item.status} />
        </section>
        <section className="panel">
          <div className="panel-heading"><h3>Agent timeline</h3><button className="small-button" onClick={onRefresh}>Refresh</button></div>
          <Timeline steps={incident.steps} />
        </section>
        <section className="panel">
          <h3>Hypotheses</h3>
          {!incident.hypotheses?.length
            ? <div className="empty compact">The agent has not recorded hypotheses yet.</div>
            : <div className="hypothesis-list">{incident.hypotheses.map((h) => <HypothesisCard key={h.id} hypothesis={h} />)}</div>}
        </section>
      </main>
      <aside className="incident-aside">
        <RpaPanel tasks={[...(incident.rpa_tasks || [])].reverse()} />
        <EscalationCard pages={[...(incident.pages || [])].reverse()} />
        <section className="panel">
          <h3>Recent guard decisions</h3>
          {!incident.actions?.length ? <div className="empty compact">No actions proposed yet.</div> : (
            incident.actions.slice().reverse().map((action) => (
              <div className="guard-card" key={action.id}>
                <div className="panel-heading"><b>{action.fix}</b><StatusPill value={action.decision} /></div>
                <p>{action.reason}</p>
                {action.result && <small>{action.result}</small>}
              </div>
            ))
          )}
        </section>
      </aside>
      <ApprovalModal
        approval={pending}
        action={pendingAction}
        hypothesis={hypothesis}
        onApprove={() => window.dispatchEvent(new CustomEvent("autoheal-approve", { detail: pending.id }))}
        onReject={() => window.dispatchEvent(new CustomEvent("autoheal-reject", { detail: pending.id }))}
      />
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [incidents, setIncidents] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [incident, setIncident] = useState(null);
  const [chartData, setChartData] = useState([]);
  const [activeTab, setActiveTab] = useState("dashboard");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [catalog, setCatalog] = useState([]);
  const [connection, setConnection] = useState("connecting");
  const [llmMode, setLlmMode] = useState("REPLAY");
  const [elapsed, setElapsed] = useState(0);

  const refreshIncidents = useCallback(async () => {
    try {
      const rows = await api.incidents();
      setIncidents(rows);
      if (rows.length) {
        setSelectedId((current) => current || rows[0].id);
      }
    } catch (error) {
      setNotice(`Backend unavailable: ${error.message}`);
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      const value = await api.health();
      setHealth(value);
      const mode = value.llm_mode?.mode || value.llm_mode || "REPLAY";
      setLlmMode(String(mode).toUpperCase());
      setChartData((current) => [...current, {
        time: timeLabel(),
        error_rate: Number(value.error_rate || 0) * 100,
        p95_ms: Number(value.p95_ms || 0),
      }].slice(-40));
    } catch (error) {
      setNotice(`Health summary unavailable: ${error.message}`);
    }
  }, []);

  const refreshIncident = useCallback(async (id = selectedId) => {
    if (!id) {
      setIncident(null);
      return;
    }
    try {
      setIncident(await api.incident(id));
    } catch (error) {
      setNotice(`Could not load incident: ${error.message}`);
    }
  }, [selectedId]);

  useEffect(() => {
    refreshIncidents();
    refreshHealth();
    api.catalog().then(setCatalog).catch(() => {});
    const timer = window.setInterval(() => {
      refreshHealth();
      refreshIncidents();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshHealth, refreshIncidents]);

  useEffect(() => {
    refreshIncident(selectedId);
  }, [selectedId, refreshIncident]);

  useEffect(() => {
    const openedAt = incident?.incident?.opened_at;
    const resolved = Number(incident?.incident?.mttr_s);
    if (Number.isFinite(resolved) && resolved > 0) {
      setElapsed(resolved);
      return undefined;
    }
    if (!openedAt) {
      setElapsed(0);
      return undefined;
    }
    const update = () => {
      const start = new Date(openedAt).getTime();
      if (Number.isFinite(start)) setElapsed(Math.max(0, (Date.now() - start) / 1000));
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [incident]);

  const handleEvent = useCallback((event) => {
    const { type, data } = event || {};
    if (type === "llm_mode") setLlmMode(String(data?.mode || "REPLAY").toUpperCase());
    if (type === "metrics") {
      setChartData((current) => [...current, {
        time: timeLabel(),
        error_rate: Number(data?.error_rate || 0) * 100,
        p95_ms: Number(data?.p95_ms || 0),
      }].slice(-40));
      setHealth((current) => current ? {
        ...current,
        error_rate: data?.error_rate ?? current.error_rate,
        p95_ms: data?.p95_ms ?? current.p95_ms,
        services: {
          ...current.services,
          db: data?.db_status === "ok" ? "healthy" : (data?.db_status || current.services?.db),
          worker: data?.worker_status || current.services?.worker,
          disk: Number(data?.disk_used_pct) >= 100 ? "down"
            : Number(data?.disk_used_pct) >= 90 ? "degraded" : "healthy",
          payment: data?.payment_status === "ok" ? "healthy" : (data?.payment_status || current.services?.payment),
        },
      } : current);
    }
    if (["incident_opened", "incident_updated", "incident_resolved", "incident_escalated"].includes(type)) {
      refreshIncidents();
      if (data?.incident_id) {
        setSelectedId(data.incident_id);
        refreshIncident(data.incident_id);
      }
    } else if (data?.incident_id && Number(data.incident_id) === Number(selectedId)) {
      refreshIncident(selectedId);
    }
  }, [refreshIncident, refreshIncidents, selectedId]);

  const sseConnection = useSSE(handleEvent);
  useEffect(() => setConnection(sseConnection), [sseConnection]);

  useEffect(() => {
    const approveListener = async (event) => {
      try {
        await api.approve(event.detail);
        setNotice("Approval recorded. The agent is resuming.");
        await refreshIncident();
      } catch (error) {
        setNotice(`Approval failed: ${error.message}`);
      }
    };
    const rejectListener = async (event) => {
      try {
        await api.reject(event.detail);
        setNotice("Rejection recorded. The agent will replan or escalate.");
        await refreshIncident();
      } catch (error) {
        setNotice(`Rejection failed: ${error.message}`);
      }
    };
    window.addEventListener("autoheal-approve", approveListener);
    window.addEventListener("autoheal-reject", rejectListener);
    return () => {
      window.removeEventListener("autoheal-approve", approveListener);
      window.removeEventListener("autoheal-reject", rejectListener);
    };
  }, [refreshIncident]);

  const inject = async (scenario, random = false) => {
    setBusy(true);
    setNotice(random ? "Injecting a hidden random sandbox fault…" : `Injecting ${scenario}…`);
    try {
      const result = random ? await api.injectFault("random") : await api.demo(scenario);
      if (result.incident_id) setSelectedId(result.incident_id);
      setActiveTab("incident");
      setNotice(result.detail || "SANDBOX fault injected.");
      await refreshIncidents();
      await refreshHealth();
    } catch (error) {
      setNotice(`Fault injection failed: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    try {
      await api.reset();
      setIncident(null);
      setSelectedId(null);
      setNotice("SANDBOX reset. Incident state cleared.");
      await refreshIncidents();
      await refreshHealth();
    } catch (error) {
      setNotice(`Reset failed: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  const activeIncident = incidents.find((item) => Number(item.id) === Number(selectedId));
  const mttr = incident?.incident?.mttr_s ?? elapsed;

  return (
    <div className="app-shell">
      <Header
        llmMode={llmMode}
        connection={connection}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
      />
      <div className="workspace">
        <aside className="sidebar">
          <div className="sidebar-title">WORKSPACE <span>LOCAL</span></div>
          <button className={`side-link ${activeTab === "dashboard" ? "active" : ""}`} onClick={() => setActiveTab("dashboard")}>
            <span>▦</span> Operations overview
          </button>
          <button className={`side-link ${activeTab === "incident" ? "active" : ""}`} onClick={() => setActiveTab("incident")}>
            <span>◉</span> Incident room
          </button>
          <button className={`side-link ${activeTab === "audit" ? "active" : ""}`} onClick={() => setActiveTab("audit")}>
            <span>≡</span> Audit log
          </button>
          <div className="sidebar-divider" />
          <div className="sidebar-title">ACTIVE INCIDENT</div>
          {incidents.length ? incidents.slice(0, 8).map((item) => (
            <button
              key={item.id}
              className={`incident-link ${Number(item.id) === Number(selectedId) ? "active" : ""}`}
              onClick={() => { setSelectedId(item.id); setActiveTab("incident"); }}
            >
              <span className="incident-link-dot" />
              <span><b>INC-{item.id}</b><small>{item.title}</small></span>
              <StatusPill value={item.status} />
            </button>
          )) : <div className="sidebar-empty">No incidents yet</div>}
          <div className="catalog-mini">
            <div className="sidebar-title">FIX CATALOG <span>{catalog.length}</span></div>
            {catalog.slice(0, 5).map((fix) => <div key={fix.name}><span>{fix.name}</span><small>{fix.risk}</small></div>)}
          </div>
          <div className="sidebar-footer">AutoHeal prototype<br />SANDBOX environment</div>
        </aside>

        <main className="main-content">
          <div className="page-heading">
            <div><div className="eyebrow">PS-12 · AGENTIC AUTO-HEAL</div><h1>{activeTab === "dashboard" ? "Operations overview" : activeTab === "incident" ? "Incident command" : "Audit log"}</h1></div>
            <div className="header-actions">
              <span className="endpoint">{API_BASE}</span>
              <a className="text-link" href={PORTAL_URL} target="_blank" rel="noreferrer">Legacy portal ↗</a>
            </div>
          </div>
          {notice && <div className="notice" role="status"><span>{notice}</span><button onClick={() => setNotice("")}>×</button></div>}
          {activeTab === "dashboard" && (
            <Dashboard
              health={health}
              chartData={chartData}
              mttr={mttr}
              onFault={(fault) => inject(fault)}
              onRandom={() => inject("random", true)}
              onReset={reset}
              busy={busy}
            />
          )}
          {activeTab === "incident" && (
            <div className="incident-selector-row">
              {incidents.length > 1 && (
                <label className="incident-select">Incident
                  <select value={selectedId || ""} onChange={(event) => setSelectedId(event.target.value)}>
                    {incidents.map((item) => <option value={item.id} key={item.id}>INC-{item.id} · {item.title}</option>)}
                  </select>
                </label>
              )}
              <button className="small-button" onClick={() => refreshIncident()}>Refresh incident</button>
            </div>
          )}
          {activeTab === "incident" && <IncidentView incident={incident} onRefresh={() => refreshIncident()} />}
          {activeTab === "audit" && (
            <section className="panel">
              <div className="panel-heading">
                <div><div className="eyebrow">CHRONOLOGICAL RECORD</div><h2>Incident audit log</h2></div>
                {incidents.length > 0 && (
                  <select value={selectedId || ""} onChange={(event) => setSelectedId(event.target.value)}>
                    {incidents.map((item) => <option value={item.id} key={item.id}>INC-{item.id} · {item.title}</option>)}
                  </select>
                )}
              </div>
              <AuditLog incident={incident} />
            </section>
          )}
          {activeTab === "dashboard" && activeIncident && (
            <section className="panel current-incident-strip">
              <div><div className="eyebrow">CURRENT INCIDENT</div><b>INC-{activeIncident.id} · {activeIncident.title}</b></div>
              <StatusPill value={activeIncident.status} />
              <button className="small-button" onClick={() => setActiveTab("incident")}>Open incident room →</button>
            </section>
          )}
          <footer className="main-footer">
            <span>Everything shown here runs in a local SANDBOX.</span>
            <span>Catalog entries: {catalog.length} · Live event stream: {connection}</span>
          </footer>
        </main>
      </div>
    </div>
  );
}