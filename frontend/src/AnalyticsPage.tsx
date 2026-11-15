import { useEffect, useState } from "react";
import { AlertTriangle, ClipboardList, Gauge } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, type Project } from "./api";

type Props = { project: Project };

export default function AnalyticsPage({ project }: Props) {
  const [data, setData] = useState<any>(null);
  useEffect(() => {
    api.get("/analytics/summary", { params: { project_id: project.id } }).then(response => setData(response.data));
  }, [project.id]);

  const series = [
    { title: "Issues by status", rows: Object.entries(data?.issues_by_status ?? {}).map(([name, value]) => ({ name, value })) },
    { title: "Incidents by severity", rows: Object.entries(data?.incidents_by_severity ?? {}).map(([name, value]) => ({ name, value })) },
  ];
  const metrics = [
    { label: "Issues", value: data?.issue_total ?? "—", hint: "Current project", Icon: ClipboardList },
    { label: "Open incidents", value: data?.open_incidents ?? "—", hint: "Current project", Icon: AlertTriangle },
    { label: "Data source", value: "PostgreSQL", hint: "Live product records", Icon: Gauge },
  ];

  return <div className="space-y-6">
    <header><div className="text-[10px] font-bold uppercase tracking-[.18em] text-[#6bd8bd]">Project metrics</div><h1 className="mt-2 text-2xl font-semibold md:text-[28px]">Analytics</h1><p className="mt-2 text-sm text-slate-500">Database counts only; these are not research experiment results.</p></header>
    <div className="grid gap-4 md:grid-cols-3">{metrics.map(({ label, value, hint, Icon }) => <div key={label} className="panel p-4"><div className="flex justify-between text-xs text-slate-500">{label}<Icon size={16}/></div><div className="mt-3 text-2xl font-semibold">{String(value)}</div><div className="mt-1 text-[10px] text-slate-600">{hint}</div></div>)}</div>
    <div className="grid gap-5 lg:grid-cols-2">{series.map(item => <section key={item.title} className="panel p-5"><h2 className="font-semibold">{item.title}</h2><div className="mt-5 h-72"><ResponsiveContainer width="100%" height="100%"><BarChart data={item.rows as any[]}><CartesianGrid stroke="#283843" vertical={false}/><XAxis dataKey="name" stroke="#82919c" tick={{ fontSize: 11 }}/><YAxis allowDecimals={false} stroke="#82919c" tick={{ fontSize: 11 }}/><Tooltip contentStyle={{ background: "#111b24", border: "1px solid #30414d", borderRadius: 10 }}/><Bar dataKey="value" fill="#53d3b6" radius={[5, 5, 0, 0]}/></BarChart></ResponsiveContainer></div></section>)}</div>
  </div>;
}
