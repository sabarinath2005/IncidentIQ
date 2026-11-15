import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ExternalLink, RefreshCw, TicketCheck } from "lucide-react";
import { api, type Project } from "./api";

export default function JiraPage({ project, show }: { project: Project; show: (message: string, error?: boolean) => void }) {
  const [integration, setIntegration] = useState<any>(null); const [mapping, setMapping] = useState<any>(null); const [links, setLinks] = useState<any[]>([]);
  const [busy, setBusy] = useState(false); const canManage = project.role === "admin";
  const load = useCallback(async () => { try { const [rows, mapped, linked] = await Promise.all([
    api.get(`/projects/${project.id}/integrations`), api.get(`/projects/${project.id}/jira/project-mapping`), api.get(`/projects/${project.id}/jira/issues`)]);
    setIntegration(rows.data.find((x: any) => x.provider === "jira") ?? null); setMapping(mapped.data); setLinks(linked.data);
  } catch (error) { show(err(error), true); } }, [project.id, show]);
  useEffect(() => { void load(); }, [load]);

  async function connect(e: FormEvent<HTMLFormElement>) { e.preventDefault(); setBusy(true); const form = new FormData(e.currentTarget);
    try { const created = await api.post(`/projects/${project.id}/integrations`, { provider: "jira", config: { base_url: form.get("base_url") } });
      await api.put(`/integrations/${created.data.id}/credentials`, { credentials: { email: form.get("email"), api_token: form.get("api_token") } });
      await api.patch(`/integrations/${created.data.id}`, { enabled: true }); show("Jira connection saved with encrypted server-side credentials."); void load();
    } catch (error) { show(err(error), true); } finally { setBusy(false); }
  }
  async function saveCredentials(e: FormEvent<HTMLFormElement>) { e.preventDefault(); const formElement = e.currentTarget; const form = new FormData(formElement);
    try { await api.put(`/integrations/${integration.id}/credentials`, { credentials: { email: form.get("email"), api_token: form.get("api_token") } }); show("Jira credentials replaced."); formElement.reset(); void load(); }
    catch (error) { show(err(error), true); }
  }
  async function test() { try { const r = await api.post(`/integrations/${integration.id}/test`); show(r.data.message, r.data.connection_test !== "CONNECTED"); void load(); } catch (error) { show(err(error), true); } }
  async function mapProject(e: FormEvent<HTMLFormElement>) { e.preventDefault(); const form = new FormData(e.currentTarget);
    try { await api.put(`/integrations/${integration.id}/jira/project-mapping`, { jira_project_key: form.get("jira_project_key"), jira_project_name: form.get("jira_project_name") }); show("Jira project mapping saved."); void load(); }
    catch (error) { show(err(error), true); }
  }
  async function sync(id: string) { try { const result = await api.post(`/external-issue-links/${id}/sync`); show(`Jira status synchronized: ${result.data.external_status}.`); void load(); } catch (error) { show(err(error), true); } }

  return <div className="space-y-6"><header><div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">APPROVED ENGINEERING FOLLOW-UP</div><h1 className="mt-2 flex items-center gap-3 text-2xl font-semibold"><TicketCheck/>Jira</h1><p className="mt-2 max-w-3xl text-sm text-slate-500">Connect Jira Cloud, map this IncidentIQ project, and create tasks only after an authorized user approves a recommendation.</p></header>
    {!integration && canManage && <form onSubmit={connect} className="panel grid gap-3 p-5 md:grid-cols-2"><h2 className="font-semibold md:col-span-2">Connect Jira Cloud</h2><label className="text-xs text-slate-400">Jira site<input className="field mt-2" name="base_url" type="url" placeholder="https://your-site.atlassian.net" required/></label><label className="text-xs text-slate-400">Account email<input className="field mt-2" name="email" type="email" autoComplete="username" required/></label><label className="text-xs text-slate-400">API token<input className="field mt-2" name="api_token" type="password" autoComplete="new-password" required/></label><p className="text-xs text-slate-500">Jira Cloud only. Credentials are encrypted by the backend and masked in all API responses.</p><button disabled={busy} className="btn btn-primary md:w-fit">{busy ? "Saving…" : "Save Jira connection"}</button></form>}
    {integration && <><section className="panel p-5"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-semibold">Connection status</h2><p className="mt-2 text-xs text-slate-500">{integration.health_status} · {integration.last_test_result} · {integration.has_credentials ? "Encrypted credentials stored" : "Credentials missing"}</p>{integration.last_error && <p className="mt-2 text-xs text-amber-200">{integration.last_error}</p>}</div>{canManage && <button className="btn btn-secondary" onClick={() => void test()}>Test Jira connection</button>}</div>
      {canManage && <form onSubmit={saveCredentials} className="mt-4 grid gap-2 md:grid-cols-2"><input className="field" name="email" type="email" placeholder="Replace account email" required/><input className="field" name="api_token" type="password" autoComplete="new-password" placeholder="Replace API token" required/><button className="btn btn-secondary md:w-fit">Replace credentials</button></form>}</section>
      {canManage && <form onSubmit={mapProject} className="panel grid gap-3 p-5 md:grid-cols-[1fr_2fr_auto]"><div className="font-semibold md:col-span-3">Jira project mapping {mapping && <span className="badge">{mapping.jira_project_key}</span>}</div><input className="field" name="jira_project_key" defaultValue={mapping?.jira_project_key ?? ""} placeholder="PROJECTKEY" pattern="[A-Z][A-Z0-9_]{1,39}" required/><input className="field" name="jira_project_name" defaultValue={mapping?.jira_project_name ?? ""} placeholder="Project display name"/><button className="btn btn-primary">Save mapping</button></form>}
      <section className="panel p-5"><h2 className="font-semibold">Linked Jira tasks</h2><div className="mt-3 space-y-2">{links.map(link => <article key={link.id} className="rounded-lg bg-[#111b24] p-3"><div className="flex flex-wrap items-center justify-between gap-2"><a className="flex items-center gap-2 text-sm text-[#76ddc4]" href={link.external_issue_url || undefined} target="_blank" rel="noreferrer">{link.external_issue_key || "Pending Jira task"}<ExternalLink size={13}/></a><span className="badge">{link.status}{link.external_status ? ` · ${link.external_status}` : ""}</span></div>{link.sync_error && <p className="mt-2 text-xs text-amber-200">{link.sync_error}</p>}{link.status === "created" && canManage && <button className="btn btn-secondary mt-2 py-2 text-xs" onClick={() => void sync(link.id)}><RefreshCw size={13}/>Sync Jira status</button>}</article>)}{!links.length && <p className="text-xs text-slate-500">No Jira tasks linked yet. Open an approved recommendation in the approval queue to create one.</p>}</div></section></>}
  </div>;
}
function err(error: unknown) { return typeof error === "object" && error && "response" in error ? (error as any).response?.data?.detail ?? "Request failed." : "Could not reach the API."; }
