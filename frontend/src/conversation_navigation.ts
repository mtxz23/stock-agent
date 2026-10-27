/** 将提问定位与对话末尾区分开，保留同一分支的后续消息。 */
import type { TaskSummary } from './api';

type NavigationTask = Pick<TaskSummary, 'id' | 'parent_id' | 'created_at'>;

/** 优先保留正在查看的分支；从旧提问进入时延伸到该分支的最新消息。 */
export function questionBranchTail(tasks: NavigationTask[], questionId: string, currentId: string | null): string {
  const byId = new Map(tasks.map(task => [task.id, task]));
  if (!byId.has(questionId)) throw new Error('待定位的提问已不存在，请刷新对话列表');
  const children = new Map<string, string[]>();
  for (const task of tasks) {
    if (!task.parent_id) continue;
    const siblings = children.get(task.parent_id) || [];
    siblings.push(task.id); children.set(task.parent_id, siblings);
  }
  const descendants = collectDescendants(questionId, children);
  const anchor = currentId && descendants.has(currentId) ? currentId : questionId;
  const leaves = [...collectDescendants(anchor, children)]
    .map(id => byId.get(id))
    .filter((task): task is NavigationTask => !!task && !children.get(task.id)?.length)
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
  if (!leaves.length) throw new Error('对话关联异常，无法恢复完整分支');
  return leaves[0].id;
}

/** 按父子关联查找后续消息，已访问记录避免异常循环导致无限遍历。 */
function collectDescendants(id: string, children: Map<string, string[]>): Set<string> {
  const pending = [id];
  const visited = new Set<string>();
  while (pending.length) {
    const current = pending.pop()!;
    if (visited.has(current)) continue;
    visited.add(current);
    pending.push(...children.get(current) || []);
  }
  return visited;
}
