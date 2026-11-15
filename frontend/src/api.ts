import axios from "axios";
export const api = axios.create({ baseURL: import.meta.env.VITE_API_URL ?? "http://localhost:8000/api/v1" });
api.interceptors.request.use((config) => {
  const token = localStorage.getItem("incident_token");
  if (token) config.headers.Authorization = "Bearer " + token;
  return config;
});
export type Project = { id: string; workspace_id: string; name: string; description: string; role?: string };
export type Issue = { id: string; key: string; title: string; type: string; status: string; priority: string; assignee_id?: string };
export type Incident = { id: string; title: string; severity: string; status: string; project_id: string; detected_at: string };
export type Run = { id: string; incident_id: string; status: string; model_id: string; created_at: string };
export type KnowledgeSearchResult = {
  chunk_id: string;
  document_id: string;
  document_title: string;
  source_kind: string;
  version: number;
  chunk_index: number;
  text: string;
  similarity_score: number;
  retrieval_method: string;
  dimensions: number;
  embedding_model: string;
};
export type AnalyticsSummary = {
  issues_by_status: Record<string, number>;
  incidents_by_severity: Record<string, number>;
  open_incidents: number;
  issue_total: number;
  investigations_by_status?: Record<string, number>;
  investigation_total?: number;
  approvals_by_status?: Record<string, number>;
  approval_total?: number;
  knowledge_docs_total?: number;
  ai_provider_mode?: string;
};
