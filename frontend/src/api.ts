/** 定义前后端契约与统一请求错误处理。 */
export interface ModelConnection {
  id: string; name: string; protocol: 'chat_completions'; base_url: string;
  model: string; max_tokens: number; max_steps: number; context_budget: number; has_api_key: boolean;
  token_parameter: 'max_tokens' | 'max_completion_tokens';
}
export type ModelForm = Omit<ModelConnection, 'id' | 'has_api_key'> & { api_key: string };
export interface TaskSummary {
  id: string; title: string; kind: 'chat' | 'research'; company: string; question: string; model_id: string; status: string;
  parent_id: string | null; created_at: string; updated_at: string; title_pending: boolean;
}
export interface Task extends TaskSummary { report: string; error: string; model_snapshot: ModelConnection }
export interface ResearchEvent {
  id: number; task_id: string; kind: string; message: string;
  data: Record<string, unknown>; created_at: string;
}
export interface Source { id: string; title: string; url: string; fetched_at: string; content_hash: string; media_type: string }
export interface TaskDetail { task: Task; events: ResearchEvent[]; sources: Source[] }

/** 把服务端校验与网络异常转换成可在页面显示的文字。 */
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch { throw new Error('无法连接本地服务，请确认后端已启动'); }
  const value = await response.json().catch(() => { throw new Error('服务返回格式异常'); });
  if (!response.ok) {
    const detail = value.detail;
    const description = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map((item: {field: string; message: string}) => `${item.field}：${item.message}`).join('；') : `请求失败（${response.status}）`;
    const diagnosticId = value.diagnostic_id || response.headers.get('X-Request-ID');
    throw new Error(`${description}${diagnosticId ? `（诊断编号：${diagnosticId}）` : ''}`);
  }
  return value as T;
}

/** 用本地时区展示带时区的后端时间。 */
export function time(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
export const active = (status: string) => ['queued', 'running'].includes(status);
export const statusLabels: Record<string, string> = { queued: '等待回复', running: '处理中', completed: '已回复', partial: '部分完成', paused: '已暂停', interrupted: '已中断', failed: '执行失败' };

/** 记忆文字及系统按需维护的内部信息。 */
export const memoryLabels = { profile: '个人信息', preference: '偏好', rule: '投资原则', fact: '公司事实', opinion: '研究观点', question: '待核实问题' };
export interface MemoryForm {
  content: string; category: keyof typeof memoryLabels; company: string; tags: string[];
  as_of: string | null; expires_on: string | null;
}
export interface MemoryEntry extends MemoryForm {
  id: string; version: number; source_kind: 'manual' | 'user' | 'model';
  source_task_id: string | null; source_available: boolean; source_title: string;
  source_quote: string; source_url: string; created_at: string; updated_at: string;
}
export interface MemoryPage { items: MemoryEntry[]; total: number }
