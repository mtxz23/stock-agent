/** 展示模型请求实际获得的记忆快照，避免误称每条都被模型采用。 */
import { memoryLabels, type MemoryEntry, type ResearchEvent } from './api';

export default function MemoryUsage({ events }: { events: ResearchEvent[] }) {
  const recalled = new Map<string, MemoryEntry>();
  for (const event of events) {
    if (event.kind !== 'memories_recalled' || !Array.isArray(event.data.items)) continue;
    for (const item of event.data.items as MemoryEntry[]) recalled.set(item.id + ':' + item.version, item);
  }
  if (!recalled.size) return null;
  return <details className="research-details"><summary><span>◇ 本次提供的长期记忆</span><span>{recalled.size} 条快照</span></summary>
    <div className="memory-snapshots"><p className="muted small">这些记忆曾提供给本次模型请求；是否采纳以回复内容为准。修改或删除记忆不改写历史快照。</p>{[...recalled].map(([key, entry]) => <article className="memory-card" key={key}><div className="memory-meta"><strong>{memoryLabels[entry.category]}</strong><span>{entry.company || '个人 / 通用'}</span><span>信息日期：{entry.as_of || '未注明'}</span></div><p className="memory-content">{entry.content}</p></article>)}</div>
  </details>;
}
