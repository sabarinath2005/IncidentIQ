import { lazy, Suspense, useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { Activity, AlertTriangle, ArrowLeft, ArrowRight, Bell, BookOpen, Check, ChevronDown, ChevronUp, ClipboardList, Cloud, FileSearch, Gauge, Github, LayoutDashboard, LogOut, Plus, Search, ShieldCheck, Sparkles, Terminal, TicketCheck, Timer, Upload, Wrench, PlugZap, X } from "lucide-react";
import { api, type Incident, type Issue, type Project, type Run, type Workspace } from "./api";
import KnowledgePage from "./KnowledgePage";
import GitHubPage from "./GitHubPage";
import JiraPage from "./JiraPage";

const Analytics = lazy(() => import("./AnalyticsPage"));

type User = { id: string; email: string; full_name: string };
type Notice = { text: string; error?: boolean };
const nav = [
  ["Overview", "/", LayoutDashboard], ["Issues", "/issues", ClipboardList],
  ["Incidents", "/incidents", AlertTriangle], ["Services", "/services", Cloud],
  ["Approvals", "/approvals", ShieldCheck], ["Analytics", "/analytics", Gauge],
  ["Audit trail", "/audit", Activity],
  ["Integrations", "/integrations", PlugZap], ["Knowledge", "/knowledge", BookOpen],
  ["GitHub", "/integrations/github", Github], ["Jira", "/integrations/jira", TicketCheck],
] as const;

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [projectId, setProjectId] = useState(localStorage.getItem("incident_project") ?? "");
  const [loading, setLoading] = useState(Boolean(localStorage.getItem("incident_token")));
  const [register, setRegister] = useState(false);
  const [authError, setAuthError] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [showNewModal, setShowNewModal] = useState(false);
  const navigate = useNavigate();
  const token = localStorage.getItem("incident_token");
  const show = (text: string, error = false) => { setNotice({ text, error }); window.setTimeout(() => setNotice(null), 3500); };
  const load = useCallback(async (targetProjectId?: string) => {
    if (!localStorage.getItem("incident_token")) { setLoading(false); return; }
    try {
      const [me, ps, ws] = await Promise.all([api.get("/auth/me"), api.get("/projects"), api.get("/workspaces")]);
      setUser(me.data); setProjects(ps.data); setWorkspaces(ws.data);
      const activeId = targetProjectId ?? projectId;
      if (targetProjectId && ps.data.some((p: Project) => p.id === targetProjectId)) {
        setProjectId(targetProjectId);
        localStorage.setItem("incident_project", targetProjectId);
      } else if (!ps.data.some((p: Project) => p.id === activeId) && ps.data.length) {
        setProjectId(ps.data[0].id);
        localStorage.setItem("incident_project", ps.data[0].id);
      }
    } catch { localStorage.removeItem("incident_token"); setUser(null); }
    finally { setLoading(false); }
  }, [projectId]);
  useEffect(() => { void load(); }, [token, load]);

  async function auth(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); setAuthError("");
    const f = new FormData(e.currentTarget);
    try {
      let r;
      if (register) r = await api.post("/auth/register", { email: f.get("email"), full_name: f.get("full_name"), password: f.get("password") });
      else {
        const body = new URLSearchParams(); body.set("username", String(f.get("email"))); body.set("password", String(f.get("password")));
        r = await api.post("/auth/login", body, { headers: { "Content-Type": "application/x-www-form-urlencoded" } });
      }
      localStorage.setItem("incident_token", r.data.access_token); setUser(r.data.user); await load();
    } catch (err) { setAuthError(errorText(err)); }
  }
  function logout() { localStorage.removeItem("incident_token"); localStorage.removeItem("incident_project"); setUser(null); setProjects([]); }

  if (loading) return <Centered>Loading your workspace…</Centered>;
  if (!user) return <Auth register={register} toggle={() => setRegister(!register)} submit={auth} error={authError}/>;
  if (!projects.length) return <Onboarding user={user} load={load} show={show} logout={logout}/>;
  const project = projects.find(p => p.id === projectId) ?? projects[0];
  return <div className="min-h-screen bg-[#0c141c] text-slate-100">
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-[248px] flex-col border-r border-[#24323e] bg-[#101922] lg:flex">
      <div className="flex h-[72px] items-center gap-3 border-b border-[#24323e] px-5"><Brand/><div><b className="text-sm">IncidentIQ</b><div className="text-[10px] uppercase tracking-[.18em] text-slate-500">Ops workspace</div></div></div>
      <div className="px-4 pt-5">
        <div className="mb-2 flex items-center justify-between px-2">
          <label htmlFor="sidebar-project-select" className="text-[10px] font-bold uppercase tracking-widest text-slate-500">Project</label>
          <button
            type="button"
            onClick={() => setShowNewModal(true)}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold text-[#69d8bd] transition hover:bg-[#1a312c]"
            aria-label="Create workspace or project"
          >
            <Plus size={12}/> New
          </button>
        </div>
        <div className="relative">
          <select
            id="sidebar-project-select"
            aria-label="Project"
            className="field appearance-none pr-8 text-sm"
            value={project.id}
            onChange={e => { setProjectId(e.target.value); localStorage.setItem("incident_project", e.target.value); }}
          >
            {projects.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}
          </select>
          <ChevronDown size={14} className="pointer-events-none absolute right-3 top-3.5 text-slate-500"/>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto px-3 pt-7"><div className="mb-3 px-3 text-[10px] font-bold uppercase tracking-widest text-slate-500">Workspace</div>{nav.map(([label,path,Icon]) => <NavLink end={path === "/"} key={path} to={path} className={({isActive}) => "mb-1 flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm " + (isActive ? "bg-[#1c3339] font-semibold text-[#72dfc3]" : "text-slate-400 hover:bg-[#182731] hover:text-white")}><Icon size={17}/>{label}</NavLink>)}<a href="http://localhost:8000/docs" target="_blank" className="mt-7 flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-slate-400">API reference</a></nav>
      <div className="m-3 rounded-xl border border-[#28423f] bg-[#132722] p-3"><div className="flex gap-2 text-xs font-semibold text-[#78ddc4]"><Sparkles size={14}/>AI investigation</div><p className="mt-2 text-[11px] leading-5 text-slate-400">Provider mode and model are recorded on each investigation. Review evidence and uncertainty before acting.</p></div>
      <div className="border-t border-[#24323e] p-4"><div className="flex items-center gap-3"><Avatar name={user.full_name}/><div className="min-w-0 flex-1"><div className="truncate text-xs font-semibold">{user.full_name}</div><div className="truncate text-[10px] text-slate-500">{user.email}</div></div><button aria-label="Sign out" onClick={logout}><LogOut size={15}/></button></div></div>
    </aside>
    <div className="lg:pl-[248px]">
      <header className="sticky top-0 z-20 flex h-[72px] items-center justify-between border-b border-[#24323e] bg-[#101922]/95 px-5 backdrop-blur md:px-8"><div className="flex items-center gap-3"><div className="lg:hidden"><Brand/></div><div><div className="text-xs text-slate-500">{project.name} / Operations</div><div className="text-sm font-semibold">Incident investigation workspace</div></div></div><div className="flex items-center gap-3"><div className="hidden items-center gap-2 rounded-lg border border-[#253440] bg-[#131e27] px-3 py-2 text-xs text-slate-500 md:flex"><Search size={14}/>Search issues and incidents</div><Bell size={17} className="text-slate-400"/></div></header>
      <main className="mx-auto max-w-[1500px] px-5 py-7 md:px-8"><Routes>
        <Route path="/" element={<Dashboard project={project} show={show}/>}/>
        <Route path="/issues" element={<Issues project={project} show={show}/>}/>
        <Route path="/issues/:issueId" element={<IssueView show={show}/>}/>
        <Route path="/incidents" element={<Incidents project={project} show={show}/>}/>
        <Route path="/incidents/:incidentId" element={<IncidentView show={show}/>}/>
        <Route path="/investigations/:runId" element={<InvestigationView show={show}/>}/>
        <Route path="/services" element={<Services project={project} show={show}/>}/>
        <Route path="/services/:serviceId" element={<ServiceView show={show}/>}/>
        <Route path="/integrations" element={<Integrations project={project} show={show}/>}/>
        <Route path="/knowledge" element={<KnowledgePage project={project} show={show}/>}/>
        <Route path="/integrations/github" element={<GitHubPage project={project} show={show}/>}/>
        <Route path="/integrations/jira" element={<JiraPage project={project} show={show}/>}/>
        <Route path="/approvals" element={<Approvals project={project} show={show}/>}/>
        <Route path="/analytics" element={<Suspense fallback={<div className="panel p-12 text-center text-sm text-slate-400">Loading analytics…</div>}><Analytics project={project}/></Suspense>}/>
        <Route path="/audit" element={<Audit project={project}/>}/>
        <Route path="*" element={<Dashboard project={project} show={show}/>}/>
      </Routes></main>
    </div>
    {showNewModal && (
      <NewWorkspaceOrProjectModal
        workspaces={workspaces}
        currentWorkspaceId={project.workspace_id}
        onClose={() => setShowNewModal(false)}
        onCreated={async (newProjectId: string, isWorkspaceAndProject: boolean) => {
          await load(newProjectId);
          setProjectId(newProjectId);
          localStorage.setItem("incident_project", newProjectId);
          navigate("/");
          show(isWorkspaceAndProject ? "Workspace and project created." : "Project created.");
          setShowNewModal(false);
        }}
        showNotice={show}
      />
    )}
    {notice && <div role="status" className={"fixed bottom-5 right-5 z-50 rounded-xl border px-4 py-3 text-sm shadow-2xl " + (notice.error ? "border-red-900 bg-[#311d20] text-red-200" : "border-[#246552] bg-[#14352e] text-emerald-100")}>{notice.error ? <AlertTriangle size={15} className="mr-2 inline"/> : <Check size={15} className="mr-2 inline"/>}{notice.text}</div>}
  </div>;
}

function NewWorkspaceOrProjectModal({
  workspaces,
  currentWorkspaceId,
  onClose,
  onCreated,
  showNotice,
}: {
  workspaces: Workspace[];
  currentWorkspaceId?: string;
  onClose: () => void;
  onCreated: (newProjectId: string, isWorkspaceAndProject: boolean) => Promise<void>;
  showNotice: (msg: string, error?: boolean) => void;
}) {
  const [mode, setMode] = useState<"project" | "workspace">(workspaces.length ? "project" : "workspace");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const form = new FormData(e.currentTarget);
    try {
      if (mode === "project") {
        const workspaceId = String(form.get("workspace_id") || workspaces[0]?.id || "");
        const name = String(form.get("name") || "").trim();
        const description = String(form.get("description") || "Incident response and engineering work.").trim();
        const res = await api.post("/projects", {
          workspace_id: workspaceId,
          name,
          description,
        });
        await onCreated(res.data.id, false);
      } else {
        const workspaceName = String(form.get("workspace_name") || "").trim();
        const projectName = String(form.get("project_name") || "").trim();
        const description = String(form.get("description") || "Incident response and engineering work.").trim();
        const wsRes = await api.post("/workspaces", { name: workspaceName });
        const pRes = await api.post("/projects", {
          workspace_id: wsRes.data.id,
          name: projectName,
          description,
        });
        await onCreated(pRes.data.id, true);
      }
    } catch (err) {
      const msg = errorText(err);
      setError(msg);
      showNotice(msg, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="panel w-full max-w-lg border border-[#273743] bg-[#111b24] p-6 shadow-2xl">
        <div className="flex items-center justify-between border-b border-[#24323e] pb-4">
          <div>
            <div className="text-[10px] font-bold uppercase tracking-widest text-[#6bd8bd]">WORKSPACE MANAGEMENT</div>
            <h2 id="modal-title" className="mt-1 text-lg font-semibold text-white">Create Workspace or Project</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:bg-[#182731] hover:text-white"
            aria-label="Close dialog"
          >
            <X size={18}/>
          </button>
        </div>

        <div className="mt-4 flex rounded-lg border border-[#24323e] bg-[#0c141c] p-1">
          <button
            type="button"
            disabled={!workspaces.length}
            onClick={() => { setMode("project"); setError(""); }}
            className={
              "flex-1 rounded-md py-2 text-xs font-semibold transition " +
              (mode === "project"
                ? "bg-[#1c3339] text-[#72dfc3]"
                : workspaces.length
                ? "text-slate-400 hover:text-white"
                : "cursor-not-allowed text-slate-600")
            }
          >
            Project in Existing Workspace
          </button>
          <button
            type="button"
            onClick={() => { setMode("workspace"); setError(""); }}
            className={
              "flex-1 rounded-md py-2 text-xs font-semibold transition " +
              (mode === "workspace" ? "bg-[#1c3339] text-[#72dfc3]" : "text-slate-400 hover:text-white")
            }
          >
            New Workspace & Project
          </button>
        </div>

        {error && (
          <div role="alert" className="mt-4 rounded-lg bg-red-950/40 p-3 text-xs text-red-200">
            {error}
          </div>
        )}

        <form onSubmit={submit} className="mt-5 space-y-4">
          {mode === "project" ? (
            <>
              <label className="block text-xs text-slate-400">
                Workspace
                <select
                  name="workspace_id"
                  className="field mt-1.5"
                  defaultValue={currentWorkspaceId ?? workspaces[0]?.id}
                  required
                >
                  {workspaces.map(w => (
                    <option key={w.id} value={w.id}>
                      {w.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs text-slate-400">
                Project name
                <input
                  name="name"
                  className="field mt-1.5"
                  required
                  placeholder="Billing Engine"
                  autoFocus
                />
              </label>
              <label className="block text-xs text-slate-400">
                Description
                <textarea
                  name="description"
                  rows={2}
                  className="field mt-1.5"
                  defaultValue="Incident response and engineering work."
                  placeholder="Incident response and engineering work."
                />
              </label>
            </>
          ) : (
            <>
              <label className="block text-xs text-slate-400">
                Workspace name
                <input
                  name="workspace_name"
                  className="field mt-1.5"
                  required
                  placeholder="Core Infrastructure"
                  autoFocus
                />
              </label>
              <label className="block text-xs text-slate-400">
                First project name
                <input
                  name="project_name"
                  className="field mt-1.5"
                  required
                  placeholder="Auth Service"
                />
              </label>
              <label className="block text-xs text-slate-400">
                Project description
                <textarea
                  name="description"
                  rows={2}
                  className="field mt-1.5"
                  defaultValue="Incident response and engineering work."
                  placeholder="Incident response and engineering work."
                />
              </label>
            </>
          )}

          <div className="mt-6 flex justify-end gap-3 border-t border-[#24323e] pt-4">
            <button type="button" onClick={onClose} className="btn btn-secondary text-xs">
              Cancel
            </button>
            <button type="submit" disabled={busy} className="btn btn-primary text-xs">
              {busy ? "Creating…" : mode === "project" ? "Create project" : "Create workspace & project"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Auth({register,toggle,submit,error}:{register:boolean;toggle:()=>void;submit:(e:FormEvent<HTMLFormElement>)=>void;error:string}) {
  return <div className="grid min-h-screen bg-[#0c141c] lg:grid-cols-2"><div className="hidden flex-col justify-between border-r border-[#24323e] bg-[radial-gradient(ellipse_at_35%_20%,#1d554b,#0d171e_70%)] p-12 lg:flex"><div className="flex items-center gap-3"><Brand/><b>IncidentIQ</b></div><div className="max-w-xl"><span className="badge badge-green">AI-assisted operations</span><h1 className="mt-7 text-5xl font-semibold leading-tight">Turn incident signals into <span className="text-[#62d8ba]">clearer decisions.</span></h1><p className="mt-5 max-w-lg text-base leading-7 text-slate-400">Investigate with traceable evidence, explicit uncertainty, and a human in control of every action.</p><div className="mt-10 grid grid-cols-3 gap-3">{["Evidence linked","Run by run","Human approved"].map(x=><div key={x} className="rounded-xl border border-white/10 bg-white/[.04] p-4 text-xs text-slate-300"><ShieldCheck size={17} className="mb-3 text-[#6bdbbf]"/>{x}</div>)}</div></div><div className="text-xs text-slate-600">Enterprise AI-Assisted Incident Response · Live Environment</div></div><div className="grid place-items-center p-6"><div className="panel w-full max-w-[410px] p-8"><div className="text-xs font-bold uppercase tracking-widest text-[#69d8bd]">{register?"Create your account":"Welcome back"}</div><h2 className="mt-2 text-2xl font-semibold">{register?"Start your workspace":"Sign in to IncidentIQ"}</h2><p className="mt-2 text-sm text-slate-500">{register?"Create an account, then set up your project.":"Use your project account to continue."}</p><form onSubmit={submit} className="mt-7 space-y-4">{register&&<label className="block text-xs text-slate-400">Full name<input name="full_name" className="field mt-2" required autoComplete="name"/></label>}<label className="block text-xs text-slate-400">Email<input name="email" type="email" className="field mt-2" required autoComplete="email"/></label><label className="block text-xs text-slate-400">Password<input name="password" type="password" minLength={10} className="field mt-2" required autoComplete={register?"new-password":"current-password"}/></label>{error&&<div role="alert" className="rounded-lg bg-red-950/40 p-3 text-xs text-red-200">{error}</div>}<button className="btn btn-primary w-full py-3">{register?"Create account":"Sign in"}<ArrowRight size={15}/></button></form><p className="mt-6 text-center text-xs text-slate-500">{register?"Already registered?":"New to the workspace?"} <button className="font-semibold text-[#66d6bb]" onClick={toggle}>{register?"Sign in":"Create account"}</button></p></div></div></div>;
}

function Onboarding({user,load,show,logout}:{user:User;load:()=>Promise<void>;show:(s:string,e?:boolean)=>void;logout:()=>void}) {
  async function create(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=new FormData(e.currentTarget);try{const w=await api.post("/workspaces",{name:f.get("workspace")});await api.post("/projects",{workspace_id:w.data.id,name:f.get("project"),description:"Incident response and engineering work."});await load();show("Workspace and project created.")}catch(err){show(errorText(err),true)}}
  return <div className="grid min-h-screen place-items-center p-6"><div className="panel w-full max-w-2xl p-8"><div className="flex justify-between"><div><span className="badge badge-green">FIRST PROJECT</span><h1 className="mt-4 text-3xl font-semibold">Create your workspace</h1><p className="mt-2 text-sm text-slate-400">Welcome, {user.full_name}. Projects require explicit membership.</p></div><button onClick={logout} className="btn btn-secondary"><LogOut size={14}/>Sign out</button></div><form onSubmit={create} className="mt-7 grid gap-4 md:grid-cols-2"><label className="text-xs text-slate-400">Workspace<input name="workspace" className="field mt-2" required placeholder="Platform Engineering"/></label><label className="text-xs text-slate-400">Project<input name="project" className="field mt-2" required placeholder="Payments API"/></label><button className="btn btn-primary md:col-span-2 md:justify-self-start">Create workspace<ArrowRight size={15}/></button></form></div></div>;
}

function Dashboard({project,show}:{project:Project;show:(s:string,e?:boolean)=>void}) {
  const [summary,setSummary]=useState<any>(null);
  const [incidents,setIncidents]=useState<Incident[]>([]);
  const [aiProvider,setAiProvider]=useState<any>(null);
  const [integrations,setIntegrations]=useState<any[]>([]);

  const load=useCallback(async()=>{
    try{
      const [s,i,p,intg]=await Promise.all([
        api.get("/analytics/summary",{params:{project_id:project.id}}),
        api.get("/projects/"+project.id+"/incidents"),
        api.get("/ai/provider"),
        api.get("/projects/"+project.id+"/integrations"),
      ]);
      setSummary(s.data);
      setIncidents(i.data);
      setAiProvider(p.data);
      setIntegrations(intg.data);
    }catch(e){show(errorText(e),true)}
  },[project.id,show]);

  useEffect(()=>{void load()},[load]);

  const isMock = !aiProvider || aiProvider.provider === "mock";
  const modelStr = String(aiProvider?.model ?? "").toLowerCase();
  const providerValue = isMock
    ? "Unconfigured"
    : modelStr.includes("gemini")
    ? "Gemini"
    : (aiProvider?.provider ? String(aiProvider.provider).toUpperCase() : "Unconfigured");
  const providerHint = isMock
    ? "Configure LLM_PROVIDER in .env"
    : (aiProvider?.model || "Live model");

  const activeIntegrationsCount = integrations.filter((x: any) => x.enabled).length;
  const integrationsValue = activeIntegrationsCount > 0 ? String(activeIntegrationsCount) : "0";
  const integrationsHint = activeIntegrationsCount > 0 ? "Connected sources" : "Configure in Integrations";

  return <div className="space-y-7">
    <Heading eyebrow="OPERATIONS OVERVIEW" title={project.name} subtitle="Incident response and engineering workload for this project." action={<Link to="/incidents" className="btn btn-primary"><Plus size={15}/>New incident</Link>}/>
    {integrations.length === 0 && (
      <div className="flex gap-3 rounded-xl border border-[#255247] bg-[#132722] px-4 py-3 text-xs leading-5 text-[#b7d9d0]">
        <Sparkles size={16} className="shrink-0 text-[#69d8bd]"/>
        <span><b className="text-[#78e1c7]">No external telemetry connected yet.</b> Configure GitHub, Alertmanager, or Jira in <Link to="/integrations" className="underline font-medium text-[#78e1c7]">Integrations</Link> to ingest live production signals.</span>
      </div>
    )}
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Metric icon={AlertTriangle} label="Active incidents" value={summary?.open_incidents??"—"} hint="Needs investigation"/>
      <Metric icon={ClipboardList} label="Tracked issues" value={summary?.issue_total??"—"} hint="Across workflows"/>
      <Metric icon={Timer} label="Investigation provider" value={providerValue} hint={providerHint}/>
      <Metric icon={Cloud} label="Service integrations" value={integrationsValue} hint={integrationsHint}/>
    </div>
    <div className="grid gap-5 xl:grid-cols-[1.3fr_.7fr]">
      <section className="panel p-5">
        <div className="mb-3 flex justify-between">
          <div><h2 className="font-semibold">Recent incidents</h2><p className="mt-1 text-xs text-slate-500">Latest detected events</p></div>
          <Link to="/incidents" className="text-xs text-[#70dec1]">View all →</Link>
        </div>
        {incidents.slice(0,6).map(i=><Link key={i.id} to={"/incidents/"+i.id} className="table-row flex items-center gap-3 py-3"><span className={"h-2 w-2 rounded-full "+(i.severity==="SEV-1"?"bg-red-400":"bg-[#54d1b5]")}/><div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{i.title}</div><div className="mt-1 text-[11px] text-slate-500">{i.severity} · {new Date(i.detected_at).toLocaleString()}</div></div><span className="badge">{i.status}</span></Link>)}
        {!incidents.length&&<Empty icon={AlertTriangle} title="No incidents yet" detail="Connect an alert source or create an incident to begin investigating."/>}
      </section>
      <section className="panel p-5">
        <h2 className="font-semibold">Issue workload</h2>
        <p className="mt-1 text-xs text-slate-500">Current workflow distribution</p>
        <div className="mt-5 space-y-3">
          {Object.entries(summary?.issues_by_status??{}).map(([name,value]:any)=><div key={name}><div className="mb-1 flex justify-between text-xs"><span className="text-slate-400">{name}</span><span>{value}</span></div><div className="h-1.5 rounded-full bg-[#24323e]"><div className="h-full rounded-full bg-[#53d3b6]" style={{width:Math.min(100,value*20)+"%"}}/></div></div>)}
          {!summary?.issue_total&&<p className="py-8 text-center text-xs text-slate-500">Create an issue to see workload here.</p>}
          <Link to="/issues" className="btn btn-secondary mt-4 w-full">Open issue board</Link>
        </div>
      </section>
    </div>
    <div className="grid gap-4 md:grid-cols-3">
      <Feature icon={FileSearch} title="Traceable evidence" text="Findings cite evidence collected for their exact run."/>
      <Feature icon={ShieldCheck} title="Review before action" text="Recommendations require human approval before simulation."/>
      <Feature icon={Activity} title="Auditable runs" text="Steps, source gaps and retry attempts are retained."/>
    </div>
  </div>;
}

function Issues({project,show}:{project:Project;show:(s:string,e?:boolean)=>void}) {
 const [rows,setRows]=useState<Issue[]>([]);const [q,setQ]=useState("");const [filter,setFilter]=useState("");const [form,setForm]=useState(false);
 const load=useCallback(()=>api.get("/projects/"+project.id+"/issues",{params:{q:q||undefined,status:filter||undefined}}).then(r=>setRows(r.data)).catch(e=>show(errorText(e),true)),[project.id,q,filter]);useEffect(()=>{void load()},[load]);
 async function create(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=new FormData(e.currentTarget);try{await api.post("/projects/"+project.id+"/issues",{type:f.get("type"),title:f.get("title"),description:f.get("description"),priority:f.get("priority")});show("Issue created.");setForm(false);void load()}catch(err){show(errorText(err),true)}}
 async function move(issue:Issue,status:string){try{await api.patch("/issues/"+issue.id,{status});show(issue.key+" moved to "+status);void load()}catch(e){show(errorText(e),true)}}
 return <div className="space-y-6"><Heading eyebrow="ENGINEERING WORK" title="Issues" subtitle="Tasks, bugs, and follow-up work." action={<button onClick={()=>setForm(!form)} className="btn btn-primary"><Plus size={15}/>New issue</button>}/>{form&&<form onSubmit={create} className="panel grid gap-3 p-5 md:grid-cols-4"><input className="field md:col-span-2" name="title" placeholder="Issue title" required/><select className="field" name="type"><option>Bug</option><option>Task</option><option>Story</option><option>Improvement</option></select><select className="field" name="priority"><option>Medium</option><option>Critical</option><option>High</option><option>Low</option></select><textarea className="field md:col-span-3" name="description" placeholder="Description"/><button className="btn btn-primary">Create issue</button></form>}
 <div className="panel overflow-hidden"><div className="flex flex-wrap gap-3 border-b border-[#24323e] p-4"><div className="relative min-w-[220px] flex-1"><Search size={15} className="absolute left-3 top-3 text-slate-500"/><input className="field pl-9" placeholder="Search issues…" value={q} onChange={e=>setQ(e.target.value)}/></div><select className="field w-auto" value={filter} onChange={e=>setFilter(e.target.value)}><option value="">All statuses</option>{["Open","In Progress","In Review","Resolved","Closed"].map(s=><option key={s}>{s}</option>)}</select><span className="self-center text-xs text-slate-500">{rows.length} issues</span></div>{rows.length?<div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-xs"><thead className="bg-[#111c25] text-[10px] uppercase tracking-wider text-slate-500"><tr>{["Key / title","Type","Priority","Status","Move"].map(s=><th className="px-4 py-3" key={s}>{s}</th>)}</tr></thead><tbody>{rows.map(i=><tr key={i.id} className="table-row"><td className="px-4 py-3"><span className="font-mono text-[10px] text-[#6bd8bd]">{i.key}</span><div className="mt-1 text-sm">{i.title}</div></td><td className="px-4 py-3 text-slate-400">{i.type}</td><td className="px-4 py-3"><span className={"badge "+(i.priority==="Critical"?"badge-red":i.priority==="High"?"badge-amber":"")}>{i.priority}</span></td><td className="px-4 py-3"><span className="badge">{i.status}</span></td><td className="px-4 py-3"><select aria-label={"Move "+i.key} value={i.status} className="field py-1.5" onChange={e=>void move(i,e.target.value)}>{[i.status,...nextStatuses(i.status)].map(s=><option key={s}>{s}</option>)}</select></td></tr>)}</tbody></table></div>:<Empty icon={ClipboardList} title="No matching issues" detail="Create a bug or task, or adjust your filters."/>}</div></div>;
}

function IssueView({show}:{show:(s:string,e?:boolean)=>void}) {
 const {issueId}=useParams();const [data,setData]=useState<any>(null);const [comment,setComment]=useState("");
 const load=useCallback(()=>api.get("/issues/"+issueId).then(r=>setData(r.data)).catch(e=>show(errorText(e),true)),[issueId]);useEffect(()=>{void load()},[load]);
 if(!data)return <Loading/>;
 async function save(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=new FormData(e.currentTarget);try{await api.patch("/issues/"+issueId,{title:f.get("title"),description:f.get("description"),priority:f.get("priority"),due_date:f.get("due_date")||null,labels:String(f.get("labels")||"").split(",").map(x=>x.trim()).filter(Boolean)});show("Issue updated.");void load()}catch(err){show(errorText(err),true)}}
 async function addComment(e:FormEvent<HTMLFormElement>){e.preventDefault();try{await api.post("/issues/"+issueId+"/comments",{body:comment});setComment("");show("Comment added.");void load()}catch(err){show(errorText(err),true)}}
 async function move(status:string){try{await api.patch("/issues/"+issueId,{status});show("Issue moved to "+status);void load()}catch(err){show(errorText(err),true)}}
 async function remove(){if(!window.confirm("Delete this issue and its history?"))return;try{await api.delete("/issues/"+issueId);window.location.href="/issues"}catch(err){show(errorText(err),true)}}
 return <div className="space-y-6"><Heading eyebrow="ISSUE DETAIL" title={data.title} subtitle={data.key+" · "+data.type+" · Created "+new Date(data.created_at).toLocaleDateString()} action={<div className="flex gap-2"><select aria-label="Issue status" className="field w-auto" value={data.status} onChange={e=>void move(e.target.value)}>{[data.status,...nextStatuses(data.status)].map(x=><option key={x}>{x}</option>)}</select><button onClick={remove} className="btn btn-secondary">Delete</button></div>}/>
 <div className="grid gap-5 xl:grid-cols-[1.1fr_.9fr]"><div className="space-y-5"><form onSubmit={save} className="panel space-y-4 p-5"><div className="grid gap-4 md:grid-cols-2"><label className="text-xs text-slate-400">Title<input className="field mt-2" name="title" defaultValue={data.title} required/></label><label className="text-xs text-slate-400">Priority<select name="priority" className="field mt-2" defaultValue={data.priority}>{["Critical","High","Medium","Low"].map(x=><option key={x}>{x}</option>)}</select></label></div><label className="block text-xs text-slate-400">Description<textarea name="description" rows={6} className="field mt-2" defaultValue={data.description}/></label><div className="grid gap-4 md:grid-cols-2"><label className="text-xs text-slate-400">Labels<input name="labels" className="field mt-2" defaultValue={(data.labels||[]).join(", ")} placeholder="api, customer-impact"/></label><label className="text-xs text-slate-400">Due date<input name="due_date" type="date" className="field mt-2" defaultValue={data.due_date?.slice(0,10)}/></label></div><button className="btn btn-primary">Save changes</button></form><section className="panel p-5"><h2 className="font-semibold">Comments</h2>{data.comments.map((c:any)=><div key={c.id} className="mt-3 rounded-lg bg-[#111b24] p-3"><p className="whitespace-pre-wrap text-xs leading-5 text-slate-300">{c.body}</p><div className="mt-2 text-[10px] text-slate-600">{new Date(c.created_at).toLocaleString()}</div></div>)}{!data.comments.length&&<p className="mt-3 text-xs text-slate-500">No comments yet.</p>}<form onSubmit={addComment} className="mt-4 flex gap-2"><input className="field" value={comment} onChange={e=>setComment(e.target.value)} placeholder="Add a comment…" required/><button className="btn btn-secondary">Post</button></form></section></div><section className="panel p-5"><h2 className="font-semibold">Activity history</h2>{data.activity.map((a:any)=><div key={a.id} className="table-row py-3"><div className="text-xs font-medium">{a.event_type}</div><div className="mt-1 text-[11px] text-slate-500">{a.old_value||"â€”"} â†’ {a.new_value||"â€”"}</div><div className="mt-1 text-[10px] text-slate-600">{new Date(a.created_at).toLocaleString()}</div></div>)}</section></div></div>;
}

function Incidents({project,show}:{project:Project;show:(s:string,e?:boolean)=>void}) {
 const [rows,setRows]=useState<Incident[]>([]);const [form,setForm]=useState(false);const load=useCallback(()=>api.get("/projects/"+project.id+"/incidents").then(r=>setRows(r.data)).catch(e=>show(errorText(e),true)),[project.id,show]);useEffect(()=>{void load()},[load]);
 async function create(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=new FormData(e.currentTarget);try{await api.post("/projects/"+project.id+"/incidents",{title:f.get("title"),description:f.get("description"),severity:f.get("severity"),service_ids:[]});show("Incident created.");setForm(false);void load()}catch(err){show(errorText(err),true)}}
 return <div className="space-y-6"><Heading eyebrow="INCIDENT RESPONSE" title="Incidents" subtitle="Investigate service disruptions with time-scoped evidence and human review." action={<button className="btn btn-primary" onClick={()=>setForm(!form)}><Plus size={15}/>New incident</button>}/>{form&&<form onSubmit={create} className="panel grid gap-3 p-5 md:grid-cols-3"><input name="title" className="field md:col-span-2" placeholder="Incident title" required/><select name="severity" className="field"><option>SEV-3</option><option>SEV-1</option><option>SEV-2</option><option>SEV-4</option></select><textarea name="description" className="field md:col-span-3" placeholder="Impact and symptoms"/><button className="btn btn-primary">Create incident</button></form>}<div className="grid gap-4">{rows.map(i=><Link key={i.id} to={"/incidents/"+i.id} className="panel flex items-center gap-4 p-5 hover:border-[#3c7268]"><div className="grid h-11 w-11 place-items-center rounded-xl bg-[#20343c] text-[#6bd8bd]"><AlertTriangle size={18}/></div><div className="min-w-0 flex-1"><div className="flex flex-wrap gap-2"><b>{i.title}</b><span className={"badge "+(i.severity==="SEV-1"?"badge-red":i.severity==="SEV-2"?"badge-amber":"")}>{i.severity}</span><span className="badge">{i.status}</span></div><div className="mt-2 text-xs text-slate-500">Detected {new Date(i.detected_at).toLocaleString()}</div></div><ArrowRight size={16} className="text-slate-500"/></Link>)}{!rows.length&&<div className="panel"><Empty icon={AlertTriangle} title="No incidents recorded" detail="Log an incident above or configure an alert integration to ingest incidents automatically."/></div>}</div></div>;
}

function IncidentView({show}:{show:(s:string,e?:boolean)=>void}) {
 const {incidentId}=useParams();const [data,setData]=useState<any>(null);const [text,setText]=useState("");const [comment,setComment]=useState("");const [busy,setBusy]=useState(false);const [collecting,setCollecting]=useState(false);
 const load=useCallback(()=>api.get("/incidents/"+incidentId).then(r=>setData(r.data)).catch(e=>show(errorText(e),true)),[incidentId]);useEffect(()=>{void load()},[load]);useEffect(()=>{if(data?.runs?.some((r:Run)=>["queued","running"].includes(r.status))){const t=window.setInterval(()=>void load(),2500);return()=>window.clearInterval(t)}},[data,load]);
 if(!data)return <Loading/>;
 const canWrite=["admin","member","engineer","incident_manager"].includes(data.viewer_role);
 async function add(e:FormEvent<HTMLFormElement>){e.preventDefault();try{await api.post("/incidents/"+incidentId+"/evidence",{source_type:"diagnostic",summary:text});setText("");show("Evidence added.");void load()}catch(err){show(errorText(err),true)}}
 async function upload(e:FormEvent<HTMLFormElement>){e.preventDefault();const form=e.currentTarget;const file=new FormData(form).get("file");if(!(file instanceof File)||!file.size)return;const body=new FormData();body.set("file",file);try{await api.post("/incidents/"+incidentId+"/evidence/upload",body,{headers:{"Content-Type":"multipart/form-data"}});form.reset();show("Evidence uploaded and hashed.");void load()}catch(err){show(errorText(err),true)}}
 async function investigate(){setBusy(true);try{const r=await api.post("/incidents/"+incidentId+"/investigations",null,{params:{idempotency_key:crypto.randomUUID()}});show("Investigation started.");if(r.data.status==="waiting_for_review")window.location.href="/investigations/"+r.data.id;else void load()}catch(e){show(errorText(e),true)}finally{setBusy(false)}}
 async function collectTelemetry(){setCollecting(true);try{await api.post("/incidents/"+incidentId+"/telemetry/collect",{});show("Telemetry collection queued.");void load()}catch(e){show(errorText(e),true)}finally{setCollecting(false)}}
 async function resolve(){const cause=window.prompt("Confirmed root cause (optional):")??"";const notes=window.prompt("Resolution notes:")??"";try{await api.post("/incidents/"+incidentId+"/resolve",{confirmed_root_cause:cause,resolution_notes:notes});show("Incident resolved.");void load()}catch(e){show(errorText(e),true)}}
 async function addIncidentComment(e:FormEvent<HTMLFormElement>){e.preventDefault();try{await api.post("/incidents/"+incidentId+"/comments",{body:comment});setComment("");show("Comment added to timeline.");void load()}catch(e){show(errorText(e),true)}}
 async function acknowledge(){try{await api.patch("/incidents/"+incidentId,{status:"acknowledged"});show("Incident acknowledged.");void load()}catch(e){show(errorText(e),true)}}
 async function assign(){const assignee=window.prompt("Project member user ID (blank clears assignment):");if(assignee===null)return;try{await api.patch("/incidents/"+incidentId,{assignee_id:assignee||null});show("Assignment updated.");void load()}catch(e){show(errorText(e),true)}}
 return <div className="space-y-6"><Heading eyebrow="INCIDENT DETAIL" title={data.title} subtitle={data.severity+" · Detected "+new Date(data.detected_at).toLocaleString()} action={<div className="flex flex-wrap gap-2">{canWrite&&data.status==="open"&&<button onClick={acknowledge} className="btn btn-secondary">Acknowledge</button>}{canWrite&&<button onClick={assign} className="btn btn-secondary">Assign</button>}{canWrite&&<button disabled={collecting} onClick={collectTelemetry} className="btn btn-secondary">{collecting?"Queueing...":"Collect telemetry"}</button>}<button disabled={!canWrite||busy||data.status==="resolved"||data.status==="closed"} onClick={investigate} className="btn btn-primary"><Sparkles size={15}/>{busy?"Starting…":"Investigate"}</button>{canWrite&&data.status==="resolved"&&<button onClick={async()=>{try{await api.post("/incidents/"+incidentId+"/close");show("Incident closed.");void load()}catch(e){show(errorText(e),true)}}} className="btn btn-secondary">Close incident</button>}<button disabled={!canWrite||["resolved","closed"].includes(data.status)} onClick={resolve} className="btn btn-secondary">Resolve incident</button></div>}/><div className="grid gap-5 xl:grid-cols-[1.3fr_.7fr]"><div className="space-y-5"><section className="panel p-5"><h2 className="font-semibold">Impact and description</h2><div className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-400">Impact: {data.impact||"Not recorded."}<p className="mt-2">{data.description||"No incident description supplied."}</p><div className="mt-2 text-xs text-slate-500">Assigned to: {data.assignee_id||"Unassigned"}</div></div></section><section className="panel p-5"><div className="mb-4 flex justify-between"><div><h2 className="font-semibold">Evidence ledger</h2><p className="mt-1 text-xs text-slate-500">Observed material attached to this incident.</p></div><span className="badge">{data.evidence.length} items</span></div>{data.evidence.map((e:any)=><div key={e.id} className="mb-3 rounded-lg border border-[#273743] bg-[#111b24] p-3"><div className="flex justify-between"><span className="badge badge-green">{e.source_type}</span><span className="text-[10px] text-slate-500">{new Date(e.observed_at).toLocaleString()}</span></div><p className="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-slate-300">{e.summary}</p><div className="mt-2 font-mono text-[9px] text-slate-600">SHA256 · {e.content_hash}</div></div>)}{!data.evidence.length&&<p className="text-xs text-slate-500">No evidence yet. Add a note or upload a diagnostic file.</p>}<form onSubmit={add} className="mt-4 flex gap-2"><input className="field" value={text} onChange={e=>setText(e.target.value)} placeholder="Paste log excerpt or diagnostic observation…" required/><button className="btn btn-secondary">Add note</button></form><form onSubmit={upload} className="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-[#24323e] p-3"><Upload size={15}/><input aria-label="Evidence file" name="file" type="file" accept=".txt,.log,.csv,.json,text/plain,application/json,text/csv" className="min-w-0 flex-1 text-xs text-slate-400"/><button className="btn btn-secondary">Upload file</button></form></section><section className="panel p-5"><h2 className="font-semibold">Investigation history</h2>{data.runs.map((r:Run)=><Link key={r.id} to={"/investigations/"+r.id} className="table-row flex justify-between py-3"><div><div className="font-mono text-xs text-[#70dec1]">{r.id.slice(0,8)} · {r.model_id}</div><div className="mt-1 text-[11px] text-slate-500">{new Date(r.created_at).toLocaleString()}</div></div><span className="badge">{r.status}</span></Link>)}{!data.runs.length&&<p className="mt-3 text-xs text-slate-500">Add evidence, then start an investigation.</p>}</section></div><aside className="space-y-5"><section className="panel p-5"><h2 className="font-semibold">Incident timeline</h2>{data.activity.map((a:any)=><div key={a.id} className="table-row border-b border-[#24323e] py-3"><div className="flex justify-between text-xs"><span className="flex items-center gap-2"><b>{a.event_type.replaceAll("_"," ")}</b><span className="badge">{a.actor_type === "system" ? "System" : "User"}</span></span><time className="text-slate-500">{new Date(a.created_at).toLocaleString()}</time></div>{a.detail?.body&&<p className="mt-2 text-xs text-slate-400">{a.detail.body}</p>}</div>)}{canWrite&&<form onSubmit={addIncidentComment} className="mt-3 flex gap-2"><input className="field" value={comment} onChange={e=>setComment(e.target.value)} placeholder="Add an incident comment" required/><button className="btn btn-secondary">Comment</button></form>}</section><section className="panel p-5"><h2 className="font-semibold">Source availability</h2><p className="mt-1 text-xs text-slate-500">Gaps are not negative evidence.</p>{data.telemetry_sources.map((x:any)=><div key={x.provider} className="mt-3 flex justify-between text-xs"><span className="text-slate-400">{x.provider}</span><span className="badge" title={x.error_code||x.status}>{x.configured?x.status:"not configured"}</span></div>)}</section><div className="rounded-xl border border-[#304146] bg-[#17252b] p-4"><div className="flex gap-2 text-xs font-semibold text-[#86d8c7]"><ShieldCheck size={15}/>Human review enabled</div><p className="mt-2 text-xs leading-5 text-slate-400">Findings need review; remediation is simulated and requires approval.</p></div></aside></div></div>;
}

function InvestigationView({show}:{show:(s:string,e?:boolean)=>void}) {
  const {runId} = useParams();
  const [data, setData] = useState<any>(null);
  const [showDebug, setShowDebug] = useState(false);
  const [approvingId, setApprovingId] = useState<string | null>(null);

  const load = useCallback(() => api.get("/investigations/" + runId)
    .then(r => setData(r.data))
    .catch(e => show(errorText(e), true)), [runId, show]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (data && ["queued", "running"].includes(data.status)) {
      const t = window.setInterval(() => void load(), 2000);
      return () => window.clearInterval(t);
    }
  }, [data, load]);

  if (!data) return <Loading/>;

  const leadingFinding = data.findings?.[0];
  const otherFindings = data.findings?.slice(1) ?? [];
  const suspectedRootCause = leadingFinding?.details_json?.suspected_root_cause || leadingFinding?.hypothesis || "Analysis in progress";
  const affectedService = leadingFinding?.details_json?.affected_service || "checkout-api";
  const explanation = leadingFinding?.details_json?.explanation || leadingFinding?.rationale || "Root cause synthesized from correlated telemetry signals and indexed runbooks.";
  const missingEvidence: string[] = leadingFinding?.details_json?.missing_evidence ?? [];
  const riskAssessment: string = leadingFinding?.details_json?.risk_assessment ?? "";

  const providerLabel = data.model_id || "gemini-2.5-flash";
  const providerModeDisplay = data.provider_mode === "live" ? "Real AI" : `${data.provider_mode || "Live"} AI`;

  async function review(id: string, status: string) {
    try {
      await api.patch("/findings/" + id + "/review", { status, note: "Reviewed in incident investigation workspace" });
      show("Finding marked as " + status.replaceAll("_", " ") + ".");
      void load();
    } catch (e) {
      show(errorText(e), true);
    }
  }

  async function requestApproval(recommendationId: string) {
    setApprovingId(recommendationId);
    try {
      await api.post("/recommendations/" + recommendationId + "/approvals");
      show("Approval request created. Proceed to Approval Queue.");
      void load();
    } catch (e) {
      show(errorText(e), true);
    } finally {
      setApprovingId(null);
    }
  }

  async function resolveIncident() {
    const defaultCause = leadingFinding?.details_json?.suspected_root_cause || leadingFinding?.hypothesis || "";
    const cause = window.prompt("Confirmed root cause:", defaultCause);
    if (cause === null) return;
    const notes = window.prompt("Resolution notes:", "Remediated based on AI root-cause analysis and verification steps.") ?? "";
    try {
      await api.post("/incidents/" + data.incident_id + "/resolve", {
        confirmed_root_cause: cause,
        resolution_notes: notes,
      });
      show("Incident marked as resolved.");
      void load();
    } catch (e) {
      show(errorText(e), true);
    }
  }

  return (
    <div className="space-y-6">
      {/* Header with Navigation and Metadata Badges */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <Link to={"/incidents/" + data.incident_id} className="mb-2 inline-flex items-center gap-1.5 text-xs font-semibold text-[#70dec1] transition hover:text-[#91e7d2]">
            <ArrowLeft size={14}/> Back to Incident
          </Link>
          <div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">AI ROOT-CAUSE INVESTIGATION</div>
          <h1 className="mt-1 text-2xl font-bold text-white md:text-[28px]">Investigation Summary</h1>
          <p className="mt-1 text-xs text-slate-400">Run ID: <span className="font-mono text-slate-300">{data.id.slice(0, 8)}</span> · Created {new Date(data.created_at).toLocaleString()}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-full border border-[#234b3f] bg-[#122822] px-3 py-1.5 text-xs font-semibold text-[#75dfc5]">
            <Sparkles size={14} className="text-[#69d8bd]"/> {providerLabel} · {providerModeDisplay}
          </span>
          <span className={"badge text-xs font-semibold capitalize " + (data.status === "succeeded" || data.status === "waiting_for_review" ? "badge-green" : "")}>
            {data.status.replaceAll("_", " ")}
          </span>
        </div>
      </div>

      {/* 1. Hero AI Summary Card (Front and Center) */}
      <section className="relative overflow-hidden rounded-2xl border border-[#2b3c47] bg-gradient-to-b from-[#14232c] to-[#0e1720] p-6 shadow-2xl md:p-8">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#243542] pb-4">
          <div className="flex items-center gap-2">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-[#183934] text-[#70dec1]">
              <Sparkles size={16}/>
            </span>
            <span className="text-xs font-bold uppercase tracking-wider text-[#70dec1]">Leading Root-Cause Hypothesis</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-slate-400">Primary Service:</span>
            <span className="rounded-md border border-[#234d42] bg-[#122823] px-2.5 py-1 font-mono text-xs font-bold text-[#6ee7b7]">
              {affectedService}
            </span>
          </div>
        </div>

        <div className="mt-5">
          <h2 className="text-xl font-bold leading-snug text-white md:text-2xl">
            {leadingFinding?.hypothesis || "Synthesizing root-cause hypotheses from evidence ledger…"}
          </h2>
          <p className="mt-3 text-sm leading-6 text-slate-300">
            {explanation}
          </p>
        </div>

        <div className="mt-5 rounded-xl border border-[#233541] bg-[#111c25]/90 p-4">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-400">Suspected Root Cause</div>
          <div className="mt-1.5 text-sm font-semibold text-[#7ce0c5]">
            {suspectedRootCause}
          </div>
          {riskAssessment && (
            <p className="mt-2 text-xs text-slate-400">
              <strong className="text-slate-300">Risk Assessment:</strong> {riskAssessment}
            </p>
          )}
        </div>

        {/* 2. Clear Human-in-the-Loop Actions */}
        {leadingFinding && (
          <div className="mt-6 flex flex-wrap items-center justify-between gap-4 rounded-xl border border-[#263845] bg-[#131f28] p-4">
            <div>
              <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-slate-300">
                <ShieldCheck size={16} className="text-[#6ee7b7]"/>
                Human-in-the-Loop Verification
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                Status: <span className="font-semibold capitalize text-white">{leadingFinding.review_status?.replaceAll("_", " ") || "Pending review"}</span>
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2.5">
              <button
                type="button"
                onClick={() => review(leadingFinding.id, "confirmed")}
                className={"btn flex items-center gap-1.5 px-4 py-2 text-xs font-semibold " + (leadingFinding.review_status === "confirmed" ? "btn-primary ring-2 ring-[#70dec1]" : "btn-primary")}
              >
                <Check size={15}/> Confirm
              </button>
              <button
                type="button"
                onClick={() => review(leadingFinding.id, "needs_more_evidence")}
                className={"btn btn-secondary flex items-center gap-1.5 border-amber-800/70 px-3.5 py-2 text-xs font-semibold text-amber-200 hover:bg-amber-950/40 " + (leadingFinding.review_status === "needs_more_evidence" ? "ring-2 ring-amber-400" : "")}
              >
                <AlertTriangle size={14}/> Need Evidence
              </button>
              <button
                type="button"
                onClick={() => review(leadingFinding.id, "rejected")}
                className={"btn btn-secondary flex items-center gap-1.5 border-rose-900/60 px-3.5 py-2 text-xs font-semibold text-rose-300 hover:bg-rose-950/40 " + (leadingFinding.review_status === "rejected" ? "ring-2 ring-rose-400" : "")}
              >
                <X size={14}/> Reject
              </button>
            </div>
          </div>
        )}
      </section>

      {/* Grid: Supporting Evidence & Remediation Actions */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Supporting Evidence Card */}
        <section className="panel flex flex-col justify-between p-6">
          <div>
            <div className="flex items-center justify-between border-b border-[#24323e] pb-3">
              <h3 className="flex items-center gap-2 font-semibold text-white">
                <FileSearch size={17} className="text-[#6ee7b7]"/>
                Key Supporting Evidence
              </h3>
              <span className="badge text-xs">{leadingFinding?.evidence?.length ?? 0} citations</span>
            </div>
            <p className="mt-2 text-xs text-slate-400">
              Correlated observations directly supporting the root-cause conclusion.
            </p>

            <div className="mt-4 space-y-3">
              {leadingFinding?.evidence?.map((e: any) => (
                <div key={e.id} className="rounded-xl border border-[#233541] bg-[#101a23] p-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="badge badge-green text-[10px] uppercase">{e.source_type}</span>
                    <span className={"badge text-[10px] font-bold uppercase " + (e.stance === "supports" ? "border-emerald-700/60 text-emerald-300" : e.stance === "contradicts" ? "border-rose-800/60 text-rose-300" : "text-slate-400")}>
                      {e.stance}
                    </span>
                  </div>
                  <p className="mt-2 text-xs leading-5 text-slate-200">
                    {e.summary}
                  </p>
                </div>
              ))}
              {(!leadingFinding?.evidence || leadingFinding.evidence.length === 0) && (
                <div className="rounded-lg border border-dashed border-[#243542] p-5 text-center text-xs text-slate-500">
                  No direct evidence linked to this finding.
                </div>
              )}
            </div>

            {missingEvidence.length > 0 && (
              <div className="mt-4 rounded-xl border border-amber-900/40 bg-amber-950/20 p-3.5 text-xs text-amber-200">
                <b className="flex items-center gap-1.5 text-amber-300">
                  <AlertTriangle size={14}/> Identified Evidence Gaps
                </b>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-amber-200/90">
                  {missingEvidence.map((x: string) => <li key={x}>{x}</li>)}
                </ul>
              </div>
            )}
          </div>
        </section>

        {/* Recommended Remediation & Actions Card */}
        <section className="panel flex flex-col justify-between p-6">
          <div>
            <div className="flex items-center justify-between border-b border-[#24323e] pb-3">
              <h3 className="flex items-center gap-2 font-semibold text-white">
                <ShieldCheck size={17} className="text-[#6ee7b7]"/>
                Remediation & Action Plan
              </h3>
              <button
                type="button"
                onClick={resolveIncident}
                className="btn btn-secondary px-2.5 py-1 text-xs font-semibold text-emerald-300 hover:text-emerald-200"
              >
                <Check size={13}/> Resolve Incident
              </button>
            </div>
            <p className="mt-2 text-xs text-slate-400">
              AI-suggested recovery procedures and gated human approval requests.
            </p>

            <div className="mt-4 space-y-4">
              {data.recommendations?.map((r: any) => (
                <div key={r.id} className="rounded-xl border border-[#233541] bg-[#101a23] p-4">
                  <div className="flex items-center justify-between gap-2">
                    <h4 className="text-sm font-semibold text-white">{r.title}</h4>
                    <span className={"badge text-[10px] font-bold uppercase " + (r.risk_level === "high" || r.risk_level === "critical" ? "badge-red" : r.risk_level === "medium" ? "badge-amber" : "badge-green")}>
                      {r.risk_level} risk
                    </span>
                  </div>
                  <p className="mt-2 text-xs leading-5 text-slate-300">{r.description}</p>
                  {r.expected_benefit && (
                    <p className="mt-2 text-xs text-slate-400">
                      <strong className="text-slate-300">Benefit:</strong> {r.expected_benefit}
                    </p>
                  )}
                  {r.verification_steps?.length > 0 && (
                    <div className="mt-2 text-xs text-slate-400">
                      <strong className="text-slate-300">Suggested Verification:</strong>
                      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-slate-400">
                        {r.verification_steps.map((step: string) => <li key={step}>{step}</li>)}
                      </ul>
                    </div>
                  )}
                  <div className="mt-4 flex items-center justify-between border-t border-[#1f2d38] pt-3">
                    <span className="text-[11px] text-slate-500">Requires human review before simulation</span>
                    <button
                      type="button"
                      disabled={approvingId === r.id}
                      onClick={() => void requestApproval(r.id)}
                      className="btn btn-primary flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold"
                    >
                      <ShieldCheck size={14}/>
                      {approvingId === r.id ? "Requesting…" : "Request Approval"}
                    </button>
                  </div>
                </div>
              ))}
              {!data.recommendations?.length && (
                <div className="rounded-lg border border-dashed border-[#243542] p-5 text-center text-xs text-slate-500">
                  No automated remediation proposed. Verify signals and resolve incident above.
                </div>
              )}
            </div>
          </div>
        </section>
      </div>

      {/* 3. Collapsible "Technical Debug Details" Drawer / Accordion */}
      <section className="overflow-hidden rounded-xl border border-[#22313d] bg-[#0f1821]">
        <button
          type="button"
          onClick={() => setShowDebug(!showDebug)}
          className="flex w-full items-center justify-between p-5 text-left transition hover:bg-[#131f29]"
          aria-expanded={showDebug}
        >
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-[#182733] text-[#72dfc3]">
              <Terminal size={17} />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-slate-200">Technical Debug Details & LangGraph Pipeline Trace</h3>
              <p className="mt-0.5 text-xs text-slate-500">Node execution states, heuristic metrics, source telemetry gaps, and raw SHA-256 hashes.</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className="badge text-[11px]">{data.steps?.length ?? 0} nodes · {data.source_statuses?.length ?? 0} sources</span>
            {showDebug ? <ChevronUp size={18} className="text-slate-400" /> : <ChevronDown size={18} className="text-slate-400" />}
          </div>
        </button>

        {showDebug && (
          <div className="space-y-6 border-t border-[#1e2a35] bg-[#0c141c] p-5">
            {/* LangGraph Pipeline Nodes Stepper */}
            <div>
              <div className="mb-3 text-[11px] font-bold uppercase tracking-wider text-slate-400">LangGraph Pipeline Nodes</div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {data.steps?.map((s: any, i: number) => (
                  <div key={s.id} className="flex items-center gap-3 rounded-lg border border-[#1e2c37] bg-[#111b24] p-3">
                    <span className="grid h-6 w-6 place-items-center rounded-full bg-[#153c34] text-xs font-bold text-[#75dfc5]">
                      {s.status === "succeeded" ? <Check size={12}/> : i + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-semibold capitalize text-slate-200">{s.step_key.replaceAll("_", " ")}</div>
                      <div className="text-[10px] text-slate-500">{s.status} · attempt {s.latest_attempt_no}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Confidence Heuristic & Telemetry */}
            <div className="rounded-lg border border-[#233541] bg-[#111c25] p-4 text-xs text-slate-400">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold text-slate-300">Confidence Scoring Model</span>
                <span className="font-mono text-xs font-semibold text-[#7ce0c5]">
                  Score: {Math.round((leadingFinding?.confidence_score ?? 0) * 100)}% ({leadingFinding?.confidence_method || "transparent heuristic"})
                </span>
              </div>
              <p className="mt-2 text-[11px] leading-5 text-slate-500">
                Confidence is a transparent heuristic score computed from citation count, evidence stance, and source verification. Historical runbook knowledge is retained separately from current-run observed evidence.
              </p>
            </div>

            {/* Telemetry Source Statuses */}
            <div>
              <div className="mb-3 text-[11px] font-bold uppercase tracking-wider text-slate-400">Telemetry Source Gaps & Query Outcomes</div>
              <div className="grid gap-2 sm:grid-cols-2">
                {data.source_statuses?.map((s: any) => (
                  <div key={s.id} className="flex items-center justify-between rounded-lg border border-[#1e2c37] bg-[#111b24] p-3 text-xs">
                    <div>
                      <div className="font-semibold text-slate-300">{s.source_type} <span className="font-normal text-slate-500">({s.source_instance_ref})</span></div>
                      <div className="mt-0.5 text-[10px] text-slate-500">{s.reason || "Source query outcome recorded"}</div>
                    </div>
                    <span className="badge text-[10px]">{s.status} · {s.result_count} items</span>
                  </div>
                ))}
                {!data.source_statuses?.length && (
                  <p className="text-xs text-slate-500">No source query statuses recorded.</p>
                )}
              </div>
            </div>

            {/* Historical RAG & SHA-256 Hashes */}
            {data.historical_context?.length > 0 && (
              <div>
                <div className="mb-3 text-[11px] font-bold uppercase tracking-wider text-slate-400">Historical Runbook Knowledge & Document Chunk Hashes</div>
                <div className="space-y-2">
                  {data.historical_context.map((c: any) => (
                    <div key={c.id} className="rounded-lg border border-[#1e2c37] bg-[#111b24] p-3 text-xs">
                      <div className="flex justify-between text-[11px]">
                        <span className="text-slate-400">{c.source_kind} · rank {c.retrieval_rank}</span>
                        <span className="badge text-[10px]">historical RAG</span>
                      </div>
                      <p className="mt-2 whitespace-pre-wrap font-mono text-xs text-slate-300">
                        {c.source_excerpt || c.source_outcome_snapshot || "No excerpt recorded"}
                      </p>
                      <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-slate-500">
                        {c.source_document_version && <span>Document v{c.source_document_version} · chunk {(c.source_chunk_index ?? 0) + 1}</span>}
                        {c.source_content_hash && <span className="font-mono">SHA256: {c.source_content_hash}</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Secondary Hypotheses (Rank #2+) */}
            {otherFindings.length > 0 && (
              <div>
                <div className="mb-3 text-[11px] font-bold uppercase tracking-wider text-slate-400">Alternative Evaluated Hypotheses</div>
                <div className="space-y-3">
                  {otherFindings.map((f: any) => (
                    <div key={f.id} className="rounded-lg border border-[#1e2c37] bg-[#111b24] p-3 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-slate-200">Rank #{f.rank}: {f.hypothesis}</span>
                        <span className="badge text-[10px]">{Math.round(f.confidence_score * 100)}% score</span>
                      </div>
                      <p className="mt-1 text-slate-400">{f.rationale}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

function Services({project,show}:{project:Project;show:(s:string,e?:boolean)=>void}) {
 const [rows,setRows]=useState<any[]>([]);const [open,setOpen]=useState(false);const load=useCallback(()=>api.get("/projects/"+project.id+"/services").then(r=>setRows(r.data)).catch(e=>show(errorText(e),true)),[project.id,show]);useEffect(()=>{void load()},[load]);
 async function create(e:FormEvent<HTMLFormElement>){e.preventDefault();const f=new FormData(e.currentTarget);try{await api.post("/projects/"+project.id+"/services",{name:f.get("name"),environment:f.get("environment"),description:f.get("description"),criticality:f.get("criticality")});show("Service added.");setOpen(false);void load()}catch(err){show(errorText(err),true)}}
 return <div className="space-y-6"><Heading eyebrow="SERVICE CATALOG" title="Services" subtitle="Map project components, criticality and ownership for incident investigation." action={<button onClick={()=>setOpen(!open)} className="btn btn-primary"><Plus size={15}/>Add service</button>}/>{open&&<form onSubmit={create} className="panel grid gap-3 p-5 md:grid-cols-2"><input name="name" className="field" placeholder="Service name" required/><select name="environment" className="field"><option>production</option><option>staging</option><option>development</option><option>testing</option></select><select name="criticality" className="field"><option value="medium">Medium criticality</option><option value="critical">Critical</option><option value="high">High</option><option value="low">Low</option></select><input name="description" className="field" placeholder="Description"/><button className="btn btn-primary">Save service</button></form>}<div className="grid gap-4 md:grid-cols-3">{rows.map(s=><Link to={'/services/'+s.id} key={s.id} className="panel p-5 hover:border-[#3b665d]"><Cloud className="text-[#67d6bb]"/><h3 className="mt-4 font-semibold">{s.name}</h3><div className="mt-2 text-xs text-slate-500">{s.environment} · {s.criticality} criticality · {s.health}</div><p className="mt-2 text-xs text-slate-500">{s.description||"No description"}</p></Link>)}{!rows.length&&<div className="panel md:col-span-3"><Empty icon={Cloud} title="No services mapped" detail="Register a service above to map components and dependencies for incident investigations."/></div>}</div></div>;
}

function ServiceView({show}:{show:(s:string,e?:boolean)=>void}) {
 const {serviceId}=useParams();const [data,setData]=useState<any>(null);
 useEffect(()=>{api.get('/services/'+serviceId).then(r=>setData(r.data)).catch(e=>show(errorText(e),true))},[serviceId]);
 if(!data)return <Loading/>;
 return <div className="space-y-6"><Heading eyebrow="SERVICE DETAIL" title={data.name} subtitle={`${data.environment} · ${data.criticality} criticality · ${data.health}`}/><p className="text-sm text-slate-400">{data.description||'No description supplied.'}</p><div className="grid gap-5 lg:grid-cols-2"><section className="panel p-5"><h2 className="font-semibold">Ownership and technology</h2><p className="mt-3 text-sm text-slate-400">Owner: {data.owner_team?.name||'Unassigned'}</p><pre className="mt-3 overflow-auto rounded-lg bg-[#111b24] p-3 text-xs text-slate-400">{JSON.stringify(data.technology,null,2)}</pre></section><section className="panel p-5"><h2 className="font-semibold">Dependencies</h2>{data.dependencies.map((d:any)=><div key={d.id} className="table-row py-3 text-sm">{d.relationship_type} <Link className="text-[#72dfc3]" to={'/services/'+d.service.id}>{d.service.name}</Link></div>)}{!data.dependencies.length&&<p className="mt-3 text-xs text-slate-500">No dependencies recorded.</p>}</section></div><section className="panel p-5"><h2 className="font-semibold">Related incidents</h2>{data.incidents.map((i:any)=><Link key={i.id} to={'/incidents/'+i.id} className="table-row flex justify-between py-3 text-sm"><span>{i.title}</span><span className="badge">{i.severity} · {i.status}</span></Link>)}{!data.incidents.length&&<p className="mt-3 text-xs text-slate-500">No incidents affect this service.</p>}</section><section className="panel p-5"><h2 className="font-semibold">Issues</h2>{data.issues.map((i:any)=><Link key={i.id} to={'/issues/'+i.id} className="table-row flex justify-between py-3 text-sm"><span>{i.key} · {i.title}</span><span className="badge">{i.status}</span></Link>)}{!data.issues.length&&<p className="mt-3 text-xs text-slate-500">No linked issues.</p>}</section></div>;
}

function Integrations({project,show}:{project:Project;show:(s:string,e?:boolean)=>void}) {
 const providers=["prometheus","alertmanager","loki","opentelemetry","tempo","grafana","github","jira","slack","pagerduty"];
 const [rows,setRows]=useState<any[]>([]),[provider,setProvider]=useState(providers[0]),[config,setConfig]=useState(""),[catalog,setCatalog]=useState<any[]>([]),[secret,setSecret]=useState("");
 const load=useCallback(()=>api.get('/projects/'+project.id+'/integrations').then(r=>setRows(r.data)).catch(e=>show(errorText(e),true)),[project.id]);useEffect(()=>{void load()},[load]);
 useEffect(()=>{api.get('/integrations/catalog').then(r=>setCatalog(r.data)).catch(()=>setCatalog([]))},[]);
 async function create(e:FormEvent){e.preventDefault();try{await api.post('/projects/'+project.id+'/integrations',{provider,config:config?JSON.parse(config):{},permission_scope:[]});setConfig('');show('Integration configuration saved.');void load()}catch(e){show(e instanceof SyntaxError?'Configuration must be valid JSON.':errorText(e),true)}}
 async function update(id:string,enabled:boolean){try{await api.patch('/integrations/'+id,{enabled});show(enabled?'Integration enabled.':'Integration disabled.');void load()}catch(e){show(errorText(e),true)}}
 async function test(id:string){try{const r=await api.post('/integrations/'+id+'/test');show(r.data.message,true);void load()}catch(e){show(errorText(e),true)}}
 async function disconnect(id:string){try{await api.delete('/integrations/'+id);show('Integration disconnected.');void load()}catch(e){show(errorText(e),true)}}
 async function replaceCredentials(id:string){try{await api.put('/integrations/'+id+'/credentials',{credentials:{token:secret}});setSecret('');show('Encrypted credentials replaced.');void load()}catch(e){show(errorText(e),true)}}
 const canManage=project.role==='admin';
 const configured=new Set(rows.map(x=>x.provider));
 return <div className="space-y-6"><Heading eyebrow="CONNECTOR FRAMEWORK" title="Integrations" subtitle="Prometheus, Alertmanager, Loki and Tempo adapters collect live project telemetry; other catalog entries remain planned."/>{canManage&&<form onSubmit={create} className="panel grid gap-3 p-5 md:grid-cols-[1fr_2fr_auto]"><select className="field" value={provider} onChange={e=>setProvider(e.target.value)}>{providers.filter(x=>!configured.has(x)).map(x=><option key={x}>{x}</option>)}</select><input className="field font-mono text-xs" value={config} onChange={e=>setConfig(e.target.value)} placeholder='Non-secret JSON config, e.g. {"base_url":"https://…"}' aria-label="Integration configuration"/><button disabled={configured.size===providers.length} className="btn btn-primary"><Plus size={15}/>Configure</button></form>}<div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{providers.map(p=>{const row=rows.find(x=>x.provider===p),meta=catalog.find(x=>x.provider===p);return <article key={p} className="panel p-5"><div className="flex items-center justify-between"><div className="flex items-center gap-3"><div className="grid h-10 w-10 place-items-center rounded-xl bg-[#193832] text-[#72ddc1]"><PlugZap size={19}/></div><div><h2 className="font-semibold capitalize">{p}</h2><div className="text-[11px] text-slate-500">{meta?.implemented?'Implemented adapter':'Adapter not implemented'}</div></div></div><span className="badge">{row?(row.enabled?'Enabled':'Disabled'):'Not connected'}</span></div>{row&&<><p className="mt-3 text-xs text-slate-400">Health: {row.health_status} · Test: {row.last_test_result} · {row.last_checked_at?new Date(row.last_checked_at).toLocaleString():'Never tested'}</p><p className="mt-2 text-[11px] text-slate-500">Credentials: {row.credentials||'Not configured'}</p>{row.last_successful_sync_at&&<p className="mt-1 text-[11px] text-slate-500">Last successful sync: {new Date(row.last_successful_sync_at).toLocaleString()}</p>}{row.last_error&&<p className="mt-2 text-xs text-amber-200">{row.last_error}</p>}{canManage&&<><form onSubmit={e=>{e.preventDefault();void replaceCredentials(row.id)}} className="mt-3 flex gap-2"><input className="field" type="password" autoComplete="new-password" value={secret} onChange={e=>setSecret(e.target.value)} placeholder="Replace credential" aria-label="Replacement integration credential"/><button className="btn btn-secondary py-2 text-xs">Save secret</button></form><div className="mt-3 flex flex-wrap gap-2"><button onClick={()=>void test(row.id)} className="btn btn-secondary py-2 text-xs">Test</button><button onClick={()=>void update(row.id,!row.enabled)} className="btn btn-secondary py-2 text-xs">{row.enabled?'Disable':'Enable'}</button><button onClick={()=>void disconnect(row.id)} className="btn btn-secondary py-2 text-xs text-red-300">Disconnect</button></div></>}</>}</article>})}</div></div>;
}

function Approvals({project,show}:{project:Project;show:(s:string,e?:boolean)=>void}) {
 const [items,setItems]=useState<any[]>([]);const load=useCallback(async()=>{try{const inc=await api.get("/projects/"+project.id+"/incidents");const all=await Promise.all(inc.data.map((i:Incident)=>api.get("/incidents/"+i.id+"/recommendations")));setItems(all.flatMap(x=>x.data.flatMap((rec:any)=>rec.approvals.map((a:any)=>({...a,recommendation:rec})))))}catch(e){show(errorText(e),true)}},[project.id]);useEffect(()=>{void load()},[load]);
 async function decide(id:string,status:string){try{await api.post("/approvals/"+id+"/decision",{status,note:"Reviewed in approval queue"});show("Request "+status);void load()}catch(e){show(errorText(e),true)}}
 async function simulate(id:string){try{const r=await api.post("/approvals/"+id+"/simulate");show(r.data.result.message);void load()}catch(e){show(errorText(e),true)}}
 return <div className="space-y-6"><Heading eyebrow="HUMAN CONTROL" title="Approval queue" subtitle="Review the exact proposed action before simulation."/><div className="rounded-xl border border-[#51402b] bg-[#2a241b] p-4 text-xs text-amber-100/80"><ShieldCheck size={15} className="mr-2 inline"/>No production changes are executed. Human approval is required before simulation.</div><div className="space-y-3">{items.map(a=><div key={a.id} className="panel p-5"><div className="flex justify-between gap-3"><div><span className="badge">{a.status}</span><h3 className="mt-3 font-semibold">{a.recommendation.title}</h3><p className="mt-2 text-xs text-slate-400">{a.recommendation.description}</p>{a.recommendation.external_issue_links?.map((link:any)=><div key={link.id} className="mt-2 text-xs"><a className="text-[#72dfc3]" href={link.external_issue_url||undefined} target="_blank" rel="noreferrer">{link.external_issue_key||"Jira task"} · {link.status}</a>{link.sync_error&&<p className="text-amber-200">{link.sync_error}</p>}</div>)}</div><span className="badge">{a.recommendation.risk_level} risk</span></div><pre className="mt-4 overflow-auto rounded-lg bg-[#111b24] p-3 text-[10px] text-slate-400">{JSON.stringify(a.action_snapshot,null,2)}</pre><div className="mt-3 flex gap-2">{a.status==="pending"?<><button onClick={()=>decide(a.id,"approved")} className="btn btn-primary py-2 text-xs"><Check size={14}/>Approve</button><button onClick={()=>decide(a.id,"rejected")} className="btn btn-secondary py-2 text-xs">Reject</button></>:a.status==="approved"?<><button onClick={()=>simulate(a.id)} className="btn btn-secondary py-2 text-xs"><Wrench size={14}/>Run simulation</button><button onClick={async()=>{try{const r=await api.post("/recommendations/"+a.recommendation.id+"/jira-issue");show("Jira issue linked: "+r.data.external_issue_key);void load()}catch(e){show(errorText(e),true)}}} className="btn btn-secondary py-2 text-xs"><TicketCheck size={14}/>Create Jira task</button></>:<span className="text-xs text-slate-500">Decision recorded: {a.decision_note}</span>}</div></div>)}{!items.length&&<div className="panel"><Empty icon={ShieldCheck} title="Approval queue is clear" detail="Request approval for an investigation recommendation."/></div>}</div></div>;
}

function Audit({project}:{project:Project}) {
 const [rows,setRows]=useState<any[]>([]);useEffect(()=>{api.get("/audit",{params:{project_id:project.id}}).then(r=>setRows(r.data))},[project.id]);
 return <div className="space-y-6"><Heading eyebrow="ACCOUNTABILITY" title="Audit trail" subtitle="Recent workspace events and human decisions."/><div className="panel p-5">{rows.map(r=><div key={r.id} className="table-row flex items-start gap-3 py-3"><Activity size={15} className="mt-1 text-[#68d6bb]"/><div className="flex-1"><div className="text-xs font-semibold">{r.action}</div><div className="mt-1 text-[11px] text-slate-500">{r.resource_type} · {r.resource_id}</div></div><time className="text-[10px] text-slate-600">{new Date(r.created_at).toLocaleString()}</time></div>)}{!rows.length&&<Empty icon={Activity} title="No audit activity yet" detail="Workspace actions will be listed here."/>}</div></div>;
}

function Heading({eyebrow,title,subtitle,action}:{eyebrow:string;title:string;subtitle:string;action?:ReactNode}) {
 return <div className="flex flex-wrap items-end justify-between gap-4"><div><div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">{eyebrow}</div><h1 className="mt-2 text-2xl font-semibold md:text-[28px]">{title}</h1><p className="mt-2 text-sm text-slate-500">{subtitle}</p></div>{action}</div>;
}
function Metric({icon:Icon,label,value,hint}:{icon:any;label:string;value:unknown;hint:string}){return <div className="panel p-4"><div className="flex justify-between text-xs text-slate-500">{label}<Icon size={16}/></div><div className="mt-3 text-2xl font-semibold">{String(value)}</div><div className="mt-1 text-[10px] text-slate-600">{hint}</div></div>}
function Feature({icon:Icon,title,text}:{icon:any;title:string;text:string}){return <div className="panel flex gap-3 p-4"><div className="grid h-9 w-9 place-items-center rounded-lg bg-[#193832] text-[#72ddc1]"><Icon size={17}/></div><div className="text-xs font-semibold">{title}<p className="mt-1 text-[11px] font-normal leading-5 text-slate-500">{text}</p></div></div>}
function Empty({icon:Icon,title,detail}:{icon:any;title:string;detail:string}){return <div className="flex flex-col items-center px-5 py-12 text-center"><div className="grid h-11 w-11 place-items-center rounded-xl bg-[#1b2b35] text-slate-500"><Icon size={19}/></div><h3 className="mt-4 text-sm font-semibold">{title}</h3><p className="mt-1 max-w-sm text-xs text-slate-500">{detail}</p></div>}
function Brand(){return <div className="grid h-9 w-9 place-items-center rounded-xl bg-[#53d3b6] text-[#0b2421]"><Activity size={18}/></div>}
function Avatar({name}:{name:string}){return <div className="grid h-9 w-9 place-items-center rounded-full bg-[#263c48] text-xs font-bold text-[#78ddc4]">{name.split(/\s+/).slice(0,2).map(x=>x[0]?.toUpperCase()).join("")}</div>}
function Centered({children}:{children:ReactNode}){return <div className="grid min-h-screen place-items-center text-sm text-slate-400">{children}</div>}
function Loading(){return <div className="panel p-12 text-center text-sm text-slate-400">Loading investigation…</div>}
function nextStatuses(value:string){const map:Record<string,string[]>={Open:["In Progress"],"In Progress":["In Review","Resolved"],"In Review":["In Progress","Resolved"],Resolved:["Closed","In Progress"],Closed:[]};return map[value]??[]}
function errorText(error:unknown):string {if(typeof error==="object"&&error&&"response" in error){return (error as any).response?.data?.detail??"Request failed."}return "Could not reach API. Check backend and database."}
