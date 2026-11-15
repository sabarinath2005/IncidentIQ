import { useCallback, useEffect, useState, type FormEvent } from "react";
import { FileSearch, RefreshCw, Trash2, Upload } from "lucide-react";
import { api, type Project } from "./api";

export default function KnowledgePage({ project, show }: { project: Project; show: (message: string, error?: boolean) => void }) {
  const [documents, setDocuments] = useState<any[]>([]);
  const [editing, setEditing] = useState<string>("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const canManage = project.role === "admin";
  const load = useCallback(() => api.get(`/projects/${project.id}/knowledge-documents`)
    .then(r => setDocuments(r.data)).catch(() => show("Could not load project knowledge documents.", true)), [project.id, show]);
  useEffect(() => { void load(); }, [load]);

  async function upload(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); setBusy(true);
    const formElement = e.currentTarget;
    const form = new FormData(formElement);
    try {
      const result = await api.post(`/projects/${project.id}/knowledge-documents`, form);
      show(result.data.deduplicated ? "This document already exists in the project." : `Document processing: ${result.data.status}.`);
      formElement.reset(); void load();
    } catch (error) { show(message(error), true); }
    finally { setBusy(false); }
  }
  async function edit(id: string) {
    if (editing === id) { setEditing(""); return; }
    try { const result = await api.get(`/knowledge-documents/${id}`); setContent(result.data.content); setEditing(id); }
    catch (error) { show(message(error), true); }
  }
  async function save(e: FormEvent<HTMLFormElement>, id: string) {
    e.preventDefault(); const form = new FormData(e.currentTarget);
    try { const r = await api.put(`/knowledge-documents/${id}`, { title: form.get("title"), source_kind: form.get("source_kind"), content }); show(`Document version ${r.data.version}: ${r.data.status}.`); setEditing(""); void load(); }
    catch (error) { show(message(error), true); }
  }
  async function retry(id: string) { try { const r = await api.post(`/knowledge-documents/${id}/retry`); show(`Document indexing: ${r.data.status}.`); void load(); } catch (error) { show(message(error), true); } }
  async function remove(id: string) { if (!window.confirm("Remove this project knowledge source?")) return; try { await api.delete(`/knowledge-documents/${id}`); show("Knowledge source removed."); void load(); } catch (error) { show(message(error), true); } }

  return <div className="space-y-6">
    <header><div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">EVIDENCE-GROUNDED RETRIEVAL</div><h1 className="mt-2 text-2xl font-semibold">Project knowledge</h1><p className="mt-2 text-sm text-slate-500">Runbooks and operational references are embedded with the configured real provider. Historical material is always labeled separately from incident evidence.</p></header>
    {canManage && <form onSubmit={upload} className="panel grid gap-3 p-5 md:grid-cols-[1fr_1fr_auto]">
      <label className="field flex items-center gap-3"><Upload size={16}/><input aria-label="Knowledge document" name="file" type="file" accept=".txt,.md,.csv,.json" required className="min-w-0 text-xs"/></label>
      <select name="source_kind" className="field"><option value="runbook">Runbook</option><option value="troubleshooting_guide">Troubleshooting guide</option><option value="architecture">Architecture documentation</option><option value="procedure">Operational procedure</option><option value="incident_summary">Historical incident summary</option></select>
      <button disabled={busy} className="btn btn-primary">{busy ? "Processing…" : "Upload and index"}</button>
      <p className="text-xs text-slate-500 md:col-span-3">Supported: UTF-8 text, Markdown, CSV and JSON. Maximum size is enforced by the backend. No document content is executed.</p>
    </form>}
    <div className="space-y-3">{documents.map(doc => <article key={doc.id} className="panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2"><FileSearch size={16} className="text-[#72dfc3]"/><h2 className="font-semibold">{doc.title}</h2><span className="badge">{doc.status}</span></div><div className="mt-2 text-xs text-slate-500">{doc.source_kind.replaceAll("_", " ")} · v{doc.version} · {doc.chunk_count} chunks · {doc.filename}</div></div>
      {canManage && <div className="flex gap-2">{doc.status === "failed" && <button className="btn btn-secondary py-2 text-xs" onClick={() => void retry(doc.id)}><RefreshCw size={14}/>Retry</button>}<button className="btn btn-secondary py-2 text-xs" onClick={() => void edit(doc.id)}>Edit</button><button aria-label={`Remove ${doc.title}`} className="btn btn-secondary py-2 text-xs text-red-300" onClick={() => void remove(doc.id)}><Trash2 size={14}/></button></div>}</div>
      {doc.status === "failed" && <p role="status" className="mt-3 text-xs text-amber-200">Not indexed: {doc.processing_error}</p>}
      {doc.status === "indexed" && <p className="mt-3 text-xs text-emerald-200">Indexed {doc.indexed_at ? new Date(doc.indexed_at).toLocaleString() : ""}. This source can be retrieved as historical context.</p>}
      {editing === doc.id && <form onSubmit={e => void save(e, doc.id)} className="mt-4 grid gap-3">
        <input name="title" className="field" defaultValue={doc.title} required/><select name="source_kind" className="field" defaultValue={doc.source_kind}><option value="runbook">Runbook</option><option value="troubleshooting_guide">Troubleshooting guide</option><option value="architecture">Architecture</option><option value="procedure">Procedure</option><option value="incident_summary">Incident summary</option></select>
        <textarea aria-label="Updated document content" className="field min-h-44" value={content} onChange={e => setContent(e.target.value)} required/><div className="flex gap-2"><button className="btn btn-primary">Save and re-index</button><button type="button" className="btn btn-secondary" onClick={() => setEditing("")}>Cancel</button></div>
      </form>}
    </article>)}{!documents.length && <div className="panel p-10 text-center text-sm text-slate-500">No project knowledge sources have been added.</div>}</div>
  </div>;
}

function message(error: unknown) { return typeof error === "object" && error && "response" in error ? (error as any).response?.data?.detail ?? "Request failed." : "Could not reach the API."; }
