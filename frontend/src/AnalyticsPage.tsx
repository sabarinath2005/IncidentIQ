import { useEffect, useState } from "react";
import { AlertTriangle, BookOpen, ClipboardList, Gauge, ShieldCheck, Sparkles } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, type AnalyticsSummary, type Project } from "./api";

type Props = { project: Project };

export default function AnalyticsPage({ project }: Props) {
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  useEffect(() => {
    api.get("/analytics/summary", { params: { project_id: project.id } }).then(response => setData(response.data));
  }, [project.id]);

  const series = [
    { title: "Issues by status", rows: Object.entries(data?.issues_by_status ?? {}).map(([name, value]) => ({ name, value })) },
    { title: "Incidents by severity", rows: Object.entries(data?.incidents_by_severity ?? {}).map(([name, value]) => ({ name, value })) },
    { title: "Investigations by status", rows: Object.entries(data?.investigations_by_status ?? {}).map(([name, value]) => ({ name, value })) },
    { title: "Approval requests by decision", rows: Object.entries(data?.approvals_by_status ?? {}).map(([name, value]) => ({ name, value })) },
  ];
  const metrics = [
    { label: "Tracked issues", value: data?.issue_total ?? "—", hint: "Current project", Icon: ClipboardList },
    { label: "Active incidents", value: data?.open_incidents ?? "—", hint: "Unresolved", Icon: AlertTriangle },
    { label: "AI investigations", value: data?.investigation_total ?? "—", hint: "Root-cause runs", Icon: Sparkles },
    { label: "Approval decisions", value: data?.approval_total ?? "—", hint: "Human-in-the-loop", Icon: ShieldCheck },
    { label: "Knowledge runbooks", value: data?.knowledge_docs_total ?? "—", hint: "1536-dim embeddings", Icon: BookOpen },
    { label: "AI provider mode", value: data?.ai_provider_mode ? data.ai_provider_mode.toUpperCase() : "DEMO", hint: "Transparent provenance", Icon: Gauge },
  ];

  return <div className="space-y-6">
    <header>
      <div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">OPERATIONAL METRICS</div>
      <h1 className="mt-2 text-2xl font-semibold md:text-[28px]">Analytics & Workload</h1>
      <p className="mt-2 text-sm text-slate-500">Live PostgreSQL workspace records, AI investigation throughput, and human decision accountability.</p>
    </header>
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      {metrics.map(({ label, value, hint, Icon }) => (
        <div key={label} className="panel p-4">
          <div className="flex justify-between text-xs text-slate-500">
            {label}
            <Icon size={16}/>
          </div>
          <div className="mt-3 text-2xl font-semibold text-slate-100">{String(value)}</div>
          <div className="mt-1 text-[10px] text-slate-500">{hint}</div>
        </div>
      ))}
    </div>
    <div className="grid gap-5 lg:grid-cols-2">
      {series.map(item => (
        <section key={item.title} className="panel p-5">
          <h2 className="font-semibold text-slate-200">{item.title}</h2>
          <div className="mt-5 h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={item.rows as any[]}>
                <CartesianGrid stroke="#283843" vertical={false}/>
                <XAxis dataKey="name" stroke="#82919c" tick={{ fontSize: 11 }}/>
                <YAxis allowDecimals={false} stroke="#82919c" tick={{ fontSize: 11 }}/>
                <Tooltip contentStyle={{ background: "#111b24", border: "1px solid #30414d", borderRadius: 10 }}/>
                <Bar dataKey="value" fill="#53d3b6" radius={[5, 5, 0, 0]}/>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      ))}
    </div>
  </div>;
}
