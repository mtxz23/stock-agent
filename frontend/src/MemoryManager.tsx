/** 在一个列表里查看和直接管理记忆。 */
import { useEffect, useState } from 'react';
import { api, type MemoryEntry, type MemoryPage } from './api';

const pageSize = 50;

export default function MemoryManager() {
  const [result, setResult] = useState<MemoryPage>({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [editor, setEditor] = useState<{ entry: MemoryEntry | null } | null>(null);
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let disposed = false;
    /** 页面打开时定期刷新，显示系统自行整理后的内容。 */
    async function load() {
      try {
        const value = await api<MemoryPage>(`/memories?offset=${offset}&limit=${pageSize}`);
        if (disposed) return;
        if (offset && value.total <= offset) {
          setOffset(Math.max(0, Math.floor((value.total - 1) / pageSize) * pageSize));
        } else setResult(value);
      } catch (cause) { if (!disposed) setError((cause as Error).message); }
      finally { if (!disposed) setLoading(false); }
    }
    void load();
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [offset, reload]);

  function edit(entry: MemoryEntry | null) {
    setEditor({ entry }); setDraft(entry?.content || ''); setError('');
  }

  /** 用户只编辑文字，系统已有的内部信息保留。 */
  async function save(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    const entry = editor?.entry;
    const payload = entry ? {
      content: draft.trim(), category: entry.category, company: entry.company,
      tags: entry.tags, as_of: entry.as_of, expires_on: entry.expires_on, version: entry.version,
    } : { content: draft.trim() };
    try {
      await api(entry ? '/memories/' + entry.id : '/memories', entry ? 'PUT' : 'POST', payload);
      setEditor(null); setDraft(''); setReload(value => value + 1);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  async function remove(entry: MemoryEntry) {
    setBusy(true); setError('');
    try {
      await api('/memories/' + entry.id + '?version=' + entry.version, 'DELETE');
      if (editor?.entry?.id === entry.id) { setEditor(null); setDraft(''); }
      setReload(value => value + 1);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="panel memory-panel">
    <div className="section-heading"><h2>记忆 <span className="muted small">{result.total} 条</span></h2><div className="actions"><button disabled={busy} onClick={() => edit(null)}>＋ 添加</button><button disabled={busy} onClick={() => { setError(''); setReload(value => value + 1); }}>刷新</button></div></div>
    <p className="muted small">系统会自行整理记忆，你也可以随时添加、编辑或删除。</p>
    {error && <div className="error" role="alert">{error}</div>}
    {editor && <form className="memory-edit" onSubmit={save}>
      <label>{editor.entry ? '编辑记忆' : '添加记忆'}<textarea autoFocus required maxLength={2000} rows={3} value={draft} onChange={event => setDraft(event.target.value)} placeholder="输入一条需要记住的内容" /></label>
      <div className="actions"><button className="primary" disabled={busy || !draft.trim()}>保存</button><button type="button" disabled={busy} onClick={() => { setEditor(null); setDraft(''); }}>取消</button></div>
    </form>}
    {loading ? <p className="muted" role="status">正在读取记忆…</p> : !result.items.length ? <p className="memory-empty muted">还没有记忆。点击“添加”，或直接在对话里告诉系统需要记住什么。</p> : <ul className="memory-list">{result.items.map(entry => <li key={entry.id}>
      <p>{entry.content}</p><div className="actions"><button disabled={busy} onClick={() => edit(entry)}>编辑</button><button disabled={busy} onClick={() => remove(entry)}>删除</button></div>
    </li>)}</ul>}
    {result.total > pageSize && <div className="actions memory-pagination"><button disabled={busy || offset === 0} onClick={() => setOffset(value => Math.max(0, value - pageSize))}>上一页</button><span className="muted small">{Math.floor(offset / pageSize) + 1} / {Math.ceil(result.total / pageSize)}</span><button disabled={busy || offset + pageSize >= result.total} onClick={() => setOffset(value => value + pageSize)}>下一页</button></div>}
  </section>;
}
