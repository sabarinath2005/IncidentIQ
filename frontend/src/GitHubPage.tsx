import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Github, GitCommitHorizontal, Link2, ShieldCheck } from "lucide-react";
import { api, type Project } from "./api";

export default function GitHubPage({ project, show }: { project: Project; show: (message: string, error?: boolean) => void }) {
  const [integration, setIntegration] = useState<any>(null);
  const [mappings, setMappings] = useState<any[]>([]);
  const [services, setServices] = useState<any[]>([]);
  const [events, setEvents] = useState<any[]>([]);
  const [repoId, setRepoId] = useState(""); const [repoName, setRepoName] = useState(""); const [serviceId, setServiceId] = useState("");
  const [busy, setBusy] = useState(false);
  const canManage = project.role === "admin";
  const load = useCallback(async () => {
    try {
      const [connections, mapped, serviceRows, eventRows] = await Promise.all([
        api.get(`/projects/${project.id}/integrations`), api.get(`/projects/${project.id}/github/repositories`),
        api.get(`/projects/${project.id}/services`), api.get(`/projects/${project.id}/github/events`),
      ]);
      setIntegration(connections.data.find((x: any) => x.provider === "github") ?? null);
      setMappings(mapped.data); setServices(serviceRows.data); setEvents(eventRows.data);
    } catch (error) { show(err(error), true); }
  }, [project.id, show]);
  useEffect(() => { void load(); }, [load]);

  async function connect(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); setBusy(true); const form = new FormData(e.currentTarget);
    try {
      const created = await api.post(`/projects/${project.id}/integrations`, { provider: "github", config: {} });
      await api.put(`/integrations/${created.data.id}/credentials`, { credentials: { token: form.get("token"), webhook_secret: form.get("webhook_secret") } });
      await api.patch(`/integrations/${created.data.id}`, { enabled: true });
      show("GitHub connection saved using encrypted server-side credentials."); void load();
    } catch (error) { show(err(error), true); }
    finally { setBusy(false); }
  }
  async function saveCredentials(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (!integration) return; const form = new FormData(e.currentTarget);
    try { await api.put(`/integrations/${integration.id}/credentials`, { credentials: { token: form.get("token"), webhook_secret: form.get("webhook_secret") } }); show("GitHub credentials replaced."); e.currentTarget.reset(); void load(); }
    catch (error) { show(err(error), true); }
  }
  async function test() { try { const r = await api.post(`/integrations/${integration.id}/test`); show(r.data.message, r.data.connection_test !== "CONNECTED"); void load(); } catch (error) { show(err(error), true); } }
  async function mapRepository(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); try { await api.post(`/integrations/${integration.id}/github/repositories`, { repository_id: repoId, repository_full_name: repoName, service_id: serviceId || null }); setRepoId(""); setRepoName(""); show("Repository mapping saved."); void load(); }
    catch (error) { show(err(error), true); }
  }
  async function removeMapping(id: string) { try { await api.delete(`/github/repositories/${id}`); show("Repository mapping removed."); void load(); } catch (error) { show(err(error), true); } }

  async function simulateEvent() {
    try {
      const repo = mappings[0]?.repository_full_name || "incidentiq/checkout-service";
      const res = await api.post(`/projects/${project.id}/github/simulate-event`, {
        event_type: "deployment_status",
        repository_full_name: repo,
        summary: `Deployment v2.4.0 completed for ${repo} (commit 8f3d1b9).`,
      });
      show(`Simulated GitHub event recorded: ${res.data.status}.`);
      void load();
    } catch (error) {
      show(err(error), true);
    }
  }

  async function quickConnectDemo() {
    setBusy(true);
    try {
      const created = await api.post(`/projects/${project.id}/integrations`, { provider: "github", config: {} });
      await api.put(`/integrations/${created.data.id}/credentials`, { credentials: { token: "ghp_demo_fine_grained_token_placeholder", webhook_secret: "demo_webhook_secret_key" } });
      await api.patch(`/integrations/${created.data.id}`, { enabled: true });
      show("GitHub demo connection saved.");
      void load();
    } catch (error) { show(err(error), true); }
    finally { setBusy(false); }
  }

  return <div className="space-y-6">
    <header><div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">ENGINEERING CHANGE SIGNALS</div><h1 className="mt-2 flex items-center gap-3 text-2xl font-semibold"><Github/>GitHub</h1><p className="mt-2 max-w-3xl text-sm text-slate-500">Receive signed push, release and deployment events. Repository/service/time-window correlation explains relevance; temporal proximity is not presented as causation.</p></header>
    {!integration && canManage && <form onSubmit={connect} className="panel grid gap-3 p-5 md:grid-cols-2">
      <div className="flex justify-between items-center md:col-span-2">
        <h2 className="font-semibold">Connect repository access</h2>
        <button type="button" onClick={quickConnectDemo} disabled={busy} className="btn btn-secondary text-xs">
          Connect Demo GitHub
        </button>
      </div>
      <label className="text-xs text-slate-400">Fine-grained read token<input className="field mt-2" name="token" type="password" autoComplete="new-password" required placeholder="GitHub token (read-only repository metadata)"/></label><label className="text-xs text-slate-400">Webhook signing secret<input className="field mt-2" name="webhook_secret" type="password" autoComplete="new-password" required placeholder="Secret configured in GitHub webhook"/></label><p className="text-xs text-slate-500 md:col-span-2">Use a fine-grained token with repository metadata read access only. Webhook events use the signing secret; no write access is needed. Both values are encrypted server-side and never returned to the browser.</p><button disabled={busy} className="btn btn-primary md:w-fit">{busy ? "Saving…" : "Save GitHub connection"}</button></form>}
    {integration && <><section className="panel p-5"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold">Connection status</h2><p className="mt-2 text-xs text-slate-500">{integration.health_status} · {integration.last_test_result} · {integration.has_credentials ? "Encrypted credentials stored" : "Credentials missing"}</p>{integration.last_error && <p className="mt-2 text-xs text-amber-200">{integration.last_error}</p>}</div>{canManage && <button className="btn btn-secondary" onClick={() => void test()}><ShieldCheck size={15}/>Test connection</button>}</div>
      {canManage && <form onSubmit={saveCredentials} className="mt-4 grid gap-2 md:grid-cols-2"><input aria-label="Replacement GitHub token" className="field" name="token" type="password" autoComplete="new-password" placeholder="Replace read token" required/><input aria-label="Replacement webhook secret" className="field" name="webhook_secret" type="password" autoComplete="new-password" placeholder="Replace webhook secret" required/><button className="btn btn-secondary md:w-fit">Replace encrypted credentials</button></form>}
      <div className="mt-4 rounded-lg border border-[#293943] bg-[#111b24] p-3 text-xs"><b>Webhook endpoint</b><code className="mt-2 block break-all text-[#76ddc4]">http://localhost:8000/api/v1/webhooks/github/{integration.id}</code><p className="mt-2 text-slate-500">Configure GitHub events: push, release, deployment and deployment_status. Require the saved webhook secret and the HMAC SHA-256 signature.</p></div>
    </section>
    {canManage && <form onSubmit={mapRepository} className="panel grid gap-3 p-5 md:grid-cols-4"><h2 className="font-semibold md:col-span-4">Map a repository to a project service</h2><input className="field" aria-label="GitHub repository ID" placeholder="Repository numeric ID" value={repoId} onChange={e => setRepoId(e.target.value)} required/><input className="field" aria-label="GitHub repository name" placeholder="owner/repository" value={repoName} onChange={e => setRepoName(e.target.value)} required/><select className="field" aria-label="Mapped IncidentIQ service" value={serviceId} onChange={e => setServiceId(e.target.value)}><option value="">Choose a service</option>{services.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select><button className="btn btn-primary"><Link2 size={15}/>Save mapping</button></form>}
    <section className="panel p-5"><h2 className="font-semibold">Repository mappings</h2><div className="mt-3 space-y-2">{mappings.map(row => <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-[#111b24] p-3 text-xs"><span className="font-medium">{row.repository_full_name}</span><span className="text-slate-500">repository {row.repository_id} · service {services.find(s => s.id === row.service_id)?.name ?? "unmapped"}</span>{canManage && <button className="text-red-300" onClick={() => void removeMapping(row.id)}>Remove</button>}</div>)}{!mappings.length && <p className="text-xs text-slate-500">No repositories are mapped.</p>}</div></section>
    <section className="panel p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 font-semibold"><GitCommitHorizontal size={17}/>Recent verified webhook deliveries</h2>
          <p className="mt-1 text-xs text-slate-500">Only signature-verified deliveries appear here.</p>
        </div>
        {canManage && (
          <button className="btn btn-secondary py-2 text-xs" onClick={() => void simulateEvent()}>
            Simulate Webhook Delivery
          </button>
        )}
      </div>
      <div className="mt-3 space-y-2">{events.map(row => <article key={row.id} className="rounded-lg bg-[#111b24] p-3"><div className="flex flex-wrap justify-between gap-2 text-xs"><b>{row.event_type} · {row.repository_full_name || "unmapped repository"}</b><span className="badge">{row.status}</span></div><p className="mt-2 text-xs text-slate-300">{row.summary}</p><p className="mt-2 text-[11px] text-slate-500">{row.correlation_reason}</p></article>)}{!events.length && <p className="text-xs text-slate-500">No GitHub deliveries received yet.</p>}</div>
    </section></>}
  </div>;
}
function err(error: unknown) { return typeof error === "object" && error && "response" in error ? (error as any).response?.data?.detail ?? "Request failed." : "Could not reach the API."; }
