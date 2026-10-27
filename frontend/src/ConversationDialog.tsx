/** 提供对话重命名及整段删除确认，原生弹窗负责焦点约束。 */
import { useEffect, useRef, useState } from 'react';

interface Props {
  action: 'rename' | 'delete';
  title: string;
  busy: boolean;
  error: string;
  onSubmit: (title: string) => Promise<void>;
  onClose: () => void;
}

export default function ConversationDialog({ action, title, busy, error, onSubmit, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState(title);
  const renaming = action === 'rename';

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  /** 请求完成之前保留弹窗，失败时允许修改或重试。 */
  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!busy) void onSubmit(draft.trim());
  }

  return <dialog ref={dialog} className="conversation-dialog" aria-labelledby="conversation-dialog-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <form onSubmit={submit}>
      <h2 id="conversation-dialog-title">{renaming ? '重命名对话' : '删除对话'}</h2>
      {renaming ? <label htmlFor="conversation-title">对话标题<input id="conversation-title" autoFocus required maxLength={80} value={draft} disabled={busy} onChange={event => setDraft(event.target.value)} /></label>
        : <p>确定删除“{title}”？这将删除所有提问和分支、报告、处理记录及原文归档，并停止正在处理的回复。删除后无法恢复。</p>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="dialog-actions">
        <button type="button" autoFocus={!renaming} disabled={busy} onClick={onClose}>取消</button>
        <button type="submit" className={renaming ? 'primary' : 'danger'} disabled={busy || (renaming && !draft.trim())}>{busy ? '处理中…' : renaming ? '保存标题' : '确认删除'}</button>
      </div>
    </form>
  </dialog>;
}
