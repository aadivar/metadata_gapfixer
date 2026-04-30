const API = (import.meta.env.VITE_API_BASE_URL as string) || "http://localhost:8000";

export type Submission = {
  id: number;
  filename: string;
  status: string;
  error?: string | null;
};

export type Section = {
  id: number;
  level: number;
  label: string;
  heading: string;
  text?: string;
  char_count: number;
  page_start?: number | null;
  page_end?: number | null;
};

export type Entity = {
  label: string;
  text: string;
  start?: number;
  end?: number;
  score?: number;
};

export async function uploadFile(file: File): Promise<Submission> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${API}/submissions`, { method: "POST", body: fd });
  if (!r.ok) throw new Error(`upload failed: ${r.status}`);
  return r.json();
}

export async function listSubmissions(): Promise<Submission[]> {
  const r = await fetch(`${API}/submissions`);
  if (!r.ok) throw new Error(`list failed: ${r.status}`);
  return r.json();
}

export async function getSubmission(id: number): Promise<Submission> {
  const r = await fetch(`${API}/submissions/${id}`);
  if (!r.ok) throw new Error(`get failed: ${r.status}`);
  return r.json();
}

export async function deleteSubmission(id: number): Promise<void> {
  const r = await fetch(`${API}/submissions/${id}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete failed: ${r.status}`);
}

export async function getSections(id: number): Promise<{ sections: Section[]; count: number }> {
  const r = await fetch(`${API}/submissions/${id}/sections`);
  if (!r.ok) throw new Error(`sections failed: ${r.status}`);
  return r.json();
}

export async function getSection(id: number, sectionId: number): Promise<Section> {
  const r = await fetch(`${API}/submissions/${id}/sections/${sectionId}`);
  if (!r.ok) throw new Error(`section failed: ${r.status}`);
  return r.json();
}

export type LayoutBox = {
  id: number;
  label: string;
  category: string;
  text: string;
  bbox: { x: number; y: number; w: number; h: number };
};

export type PageInfo = { page: number; w_px: number; h_px: number; box_count: number };

export async function getPages(id: number): Promise<{ page_count: number; dpi: number; pages: PageInfo[] }> {
  const r = await fetch(`${API}/submissions/${id}/pages`);
  if (!r.ok) {
    if (r.status === 404) return { page_count: 0, dpi: 150, pages: [] };
    throw new Error(`pages failed: ${r.status}`);
  }
  return r.json();
}

export async function getPageBoxes(id: number, pageNo: number): Promise<{ page: number; w_px: number; h_px: number; boxes: LayoutBox[] }> {
  const r = await fetch(`${API}/submissions/${id}/pages/${pageNo}/boxes`);
  if (!r.ok) throw new Error(`page boxes failed: ${r.status}`);
  return r.json();
}

export function pageImageUrl(id: number, pageNo: number): string {
  return `${API}/submissions/${id}/pages/${pageNo}/image`;
}

export async function getLabelPresets(): Promise<Record<string, Record<string, string>>> {
  const r = await fetch(`${API}/submissions/presets/labels`);
  if (!r.ok) throw new Error(`presets failed: ${r.status}`);
  return r.json();
}

export async function runNer(
  id: number,
  body: { text: string; labels?: Record<string, string>; preset?: string }
): Promise<{ entities: Entity[]; label_count: number; char_count: number }> {
  const r = await fetch(`${API}/submissions/${id}/ner`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`ner failed: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function saveEntitiesSnapshot(
  id: number,
  perSection: Record<string, Entity[]>,
  notes?: string
): Promise<void> {
  const r = await fetch(`${API}/submissions/${id}/entities`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ per_section: perSection, notes }),
  });
  if (!r.ok) throw new Error(`save entities failed: ${r.status}`);
}

export async function reconcile(id: number): Promise<any> {
  const r = await fetch(`${API}/submissions/${id}/reconcile`, { method: "POST" });
  if (!r.ok) throw new Error(`reconcile failed: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function getMetadata(id: number): Promise<any> {
  const r = await fetch(`${API}/submissions/${id}/metadata`);
  if (!r.ok) throw new Error(`metadata failed: ${r.status}`);
  return r.json();
}

export async function putMetadata(id: number, metadata: any): Promise<void> {
  const r = await fetch(`${API}/submissions/${id}/metadata`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(metadata),
  });
  if (!r.ok) throw new Error(`save failed: ${r.status}`);
}

export async function buildXml(id: number): Promise<{ path: string }> {
  const r = await fetch(`${API}/submissions/${id}/xml`, { method: "POST" });
  if (!r.ok) throw new Error(`xml build failed: ${r.status}`);
  return r.json();
}

export function xmlDownloadUrl(id: number): string {
  return `${API}/submissions/${id}/xml`;
}

// ───────────────────────── Scorecard / autofix ─────────────────────────

export type Tier = "T0" | "T1" | "T2" | "T3";
export type Bucket = "high" | "medium" | "manual";

export type FieldScore = {
  key: string;
  label: string;
  tier: Tier;
  weight: number;
  bucket: Bucket;
  status: "present" | "missing";
  value_preview?: string | null;
  autofix_action?: string | null;
  why: string;
};

export type TierScore = {
  tier: Tier;
  label: string;
  score: number;
  fields_present: number;
  fields_total: number;
};

export type Scorecard = {
  composite: number;
  interpretation: string;
  tiers: TierScore[];
  fields: FieldScore[];
  high_impact: FieldScore[];
  medium: FieldScore[];
  manual: FieldScore[];
  facts_summary: Record<string, any>;
};

export async function getScore(id: number): Promise<Scorecard> {
  const r = await fetch(`${API}/submissions/${id}/score`);
  if (!r.ok) throw new Error(`score failed: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function autofix(id: number, action: string): Promise<{ report: any; score: Scorecard }> {
  const r = await fetch(`${API}/submissions/${id}/autofix`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error(`autofix failed: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function autofixAll(id: number): Promise<{ reports: any[]; score: Scorecard }> {
  const r = await fetch(`${API}/submissions/${id}/autofix/all`, { method: "POST" });
  if (!r.ok) throw new Error(`autofix-all failed: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function getCost(id: number): Promise<{ total_usd: number; calls: any[] }> {
  const r = await fetch(`${API}/submissions/${id}/cost`);
  if (!r.ok) throw new Error(`cost failed: ${r.status}`);
  return r.json();
}

export async function getFactsheet(id: number): Promise<any> {
  const r = await fetch(`${API}/submissions/${id}/factsheet`);
  if (!r.ok) throw new Error(`factsheet failed: ${r.status}`);
  return r.json();
}
