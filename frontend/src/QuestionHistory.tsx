/** 独立展示整段对话的提问，支持搜索和定位，避免挤占侧栏。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { time, type TaskSummary } from './api';

interface Props {
  title: string;
  turns: TaskSummary[];
  selectedId: string | null;
  disabled: boolean;
  onSelect: (id: string) => void;
  onClose: () => void;
}

export default function QuestionHistory({ title, turns, selectedId, disabled, onSelect, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState('');
  const ordered = useMemo(() => [...turns].sort((a, b) => a.created_at.localeCompare(b.created_at)), [turns]);
  const needle = query.trim().toLocaleLowerCase();
  const matches = ordered.map((task, index) => ({ task, number: index + 1 }))
    .filter(({ task }) => (task.question || '首次消息').toLocaleLowerCase().includes(needle));

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  /** 关闭列表后交给主页面恢复对应分支并定位原提问。 */
  function choose(id: string) {
    if (disabled) return;
    onClose(); onSelect(id);
  }

  return <dialog ref={dialog} className="question-history-dialog" aria-labelledby="question-history-title" onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <header className="question-history-header">
      <div><h2 id="question-history-title">提问记录 <span>{turns.length}</span></h2><p>{title}</p></div>
      <button type="button" className="question-history-close" aria-label="关闭提问记录" onClick={onClose}>×</button>
    </header>
    <label className="question-history-search"><svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5" /><path d="m13 13 4 4" /></svg><input autoFocus aria-label="搜索提问" placeholder="搜索这段对话的提问..." value={query} onChange={event => setQuery(event.target.value)} /></label>
    <div className="question-history-list">
      {!matches.length ? <p className="question-history-empty">没有找到匹配的提问，试试其他关键词。</p> : matches.map(({ task, number }) => <button type="button" key={task.id} disabled={disabled} className={'question-record' + (task.id === selectedId ? ' current' : '')} aria-label={'定位第' + number + '条提问：' + (task.question || '首次消息')} onClick={() => choose(task.id)}>
        <span className="question-number">{String(number).padStart(2, '0')}</span>
        <span className="question-record-body"><span className="question-record-text">{task.question || '首次消息'}</span><span className="question-record-meta"><time>{time(task.created_at)}</time><span>{task.id === selectedId ? '当前提问' : '定位原消息 ↗'}</span></span></span>
      </button>)}
    </div>
    <footer className="question-history-footer"><span>{needle ? matches.length + ' 条匹配 / ' : ''}共 {turns.length} 条提问，包含所有分支</span><span>点击定位原消息</span></footer>
  </dialog>;
}
