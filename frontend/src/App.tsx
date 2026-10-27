/** 管理研究对话、关联历史与实时任务订阅。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { active, api, statusLabels, time, type ModelConnection, type ResearchEvent, type TaskDetail, type TaskSummary } from './api';
import ModelSettings from './ModelSettings';
import Conversation from './Conversation';
import Composer from './Composer';
import ConversationDialog from './ConversationDialog';
import ConversationMenu from './ConversationMenu';
import QuestionHistory from './QuestionHistory';
import { questionBranchTail } from './conversation_navigation';
import MemoryManager from './MemoryManager';
import './memory.css';

export default function App() {
  const [page, setPage] = useState<'research' | 'models' | 'memory'>('research');
  const [models, setModels] = useState<ModelConnection[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(() => {
    const id = window.location.hash.replace('#task=', '');
    return /^[a-f0-9]{32}$/.test(id) ? id : null;
  });
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [ancestors, setAncestors] = useState<TaskDetail[]>([]);
  const [message, setMessage] = useState('');
  const [modelId, setModelId] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [reload, setReload] = useState(0);
  const [management, setManagement] = useState<{ action: 'rename' | 'delete'; id: string; title: string; ids: string[] } | null>(null);
  const [managementError, setManagementError] = useState('');
  const [questionRoot, setQuestionRoot] = useState<string | null>(null);
  const [questionJump, setQuestionJump] = useState<{ id: string; request: number } | null>(null);
  const jumpSequence = useRef(0);
  const conversations = useMemo(() => groupConversations(tasks), [tasks]);
  const questionGroup = conversations.find(group => group.root === questionRoot);
  const titlesPending = tasks.some(task => task.title_pending);
  const selectedTitle = tasks.find(task => task.id === selected)?.title || detail?.task.title;

  /** 保留已选择的有效模型，排除未填写密钥的连接。 */
  const refreshModels = useCallback(async () => {
    const rows = await api<ModelConnection[]>('/models');
    setModels(rows);
    const available = rows.filter(row => row.has_api_key);
    setModelId(current => available.some(row => row.id === current) ? current : available[0]?.id || '');
  }, []);
  const refreshTasks = useCallback(async () => { setTasks(await api<TaskSummary[]>('/tasks')); }, []);
  useEffect(() => {
    Promise.all([refreshModels(), refreshTasks()]).catch(cause => setError(cause.message));
  }, [refreshModels, refreshTasks]);
  useEffect(() => {
    if (!titlesPending) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    /** 回复完成后命名仍在后台运行，只在有待生成标题时同步列表。 */
    async function pollTitles() {
      try {
        const rows = await api<TaskSummary[]>('/tasks');
        if (!disposed) setTasks(rows);
      } catch (cause) { if (!disposed) setError((cause as Error).message); }
      if (!disposed) timer = setTimeout(pollTitles, 1000);
    }
    timer = setTimeout(pollTitles, 1000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [titlesPending]);
  useEffect(() => {
    window.history.replaceState(null, '', window.location.pathname + window.location.search + (selected ? '#task=' + selected : ''));
  }, [selected]);

  useEffect(() => {
    setDetail(null); setAncestors([]); setConnected(false);
    if (!selected) return;
    let disposed = false;
    let stream: EventSource | undefined;

    /** 回溯当前提问的父记录，恢复这条对话分支。 */
    async function load() {
      const loaded = await api<TaskDetail>('/tasks/' + selected);
      if (disposed) return;
      const previous: TaskDetail[] = [];
      const visited = new Set([loaded.task.id]);
      let parent = loaded.task.parent_id;
      while (parent) {
        if (visited.has(parent)) throw new Error('研究记录的关联关系异常，无法恢复对话');
        visited.add(parent);
        const row = await api<TaskDetail>('/tasks/' + parent);
        if (disposed) return;
        previous.unshift(row); parent = row.task.parent_id;
      }
      setAncestors(previous); setDetail(loaded);
      if (!active(loaded.task.status)) { await refreshTasks(); return; }
      stream = new EventSource('/api/tasks/' + selected + '/events?after=' + (loaded.events.at(-1)?.id || 0));
      stream.onopen = () => { if (!disposed) setConnected(true); };
      stream.onerror = () => {
        if (disposed) return;
        setConnected(false);
        api<TaskDetail>('/tasks/' + selected).then(latest => {
          if (!disposed && !active(latest.task.status)) {
            stream?.close(); setDetail(latest);
            refreshTasks().catch(cause => setError(cause.message));
          }
        }).catch(cause => { if (!disposed) setError(cause.message); });
      };
      stream.onmessage = event => {
        if (disposed) return;
        let row: ResearchEvent;
        try { row = JSON.parse(event.data) as ResearchEvent; }
        catch { stream?.close(); setError('进展事件格式异常，请重新打开对话'); return; }
        setDetail(current => current && { ...current, events: current.events.some(item => item.id === row.id) ? current.events : [...current.events, row] });
        if (['started', 'tool_finished', 'completed', 'partial', 'failed', 'paused', 'interrupted'].includes(row.kind)) {
          Promise.all([api<TaskDetail>('/tasks/' + selected), refreshTasks()]).then(([latest]) => {
            if (!disposed) setDetail(current => {
              const merged = new Map(latest.events.map(item => [item.id, item]));
              current?.events.forEach(item => merged.set(item.id, item));
              const fresh = current && current.task.updated_at > latest.task.updated_at ? current : latest;
              return { ...fresh, events: [...merged.values()].sort((a, b) => a.id - b.id) };
            });
          }).catch(cause => { if (!disposed) setError(cause.message); });
        }
        if (['completed', 'partial', 'failed', 'paused', 'interrupted'].includes(row.kind)) {
          stream?.close(); setConnected(false);
        }
      };
    }
    load().catch(cause => { if (!disposed) setError(cause.message); });
    return () => { disposed = true; stream?.close(); };
  }, [selected, reload, refreshTasks]);

  /** 统一发送首次研究和追问，失败时保留输入内容。 */
  async function send() {
    if (busy || (selected && !detail)) return;
    setBusy(true); setError('');
    try {
      const result = await api<{ id: string }>('/tasks', 'POST', {
        message: message.trim(), model_id: modelId, parent_id: selected,
      });
      setSelected(result.id); setQuestionJump(null); setMessage('');
      // 已发送成功后单独刷新列表，避免刷新失败导致重复提交。
      refreshTasks().catch(cause => setError(cause.message));
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  /** 暂停或恢复当前回复，保留对话和草稿。 */
  async function action(name: 'pause' | 'resume') {
    if (!selected) return;
    setBusy(true); setError('');
    try {
      await api('/tasks/' + selected + '/' + name, 'POST');
      if (name === 'resume') setReload(current => current + 1);
      else setDetail(await api<TaskDetail>('/tasks/' + selected));
      refreshTasks().catch(cause => setError(cause.message));
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  /** 管理整段对话，成功后立即更新列表和当前标题。 */
  async function manageConversation(title: string) {
    if (!management || busy) return;
    const target = management;
    setBusy(true); setManagementError('');
    try {
      if (target.action === 'rename') {
        const result = await api<{ title: string }>('/conversations/' + target.id, 'PATCH', { title });
        const update = (row: TaskDetail) => target.ids.includes(row.task.id)
          ? { ...row, task: { ...row.task, title: result.title } } : row;
        setTasks(current => current.map(task => target.ids.includes(task.id) ? { ...task, title: result.title, title_pending: false } : task));
        setDetail(current => current ? update(current) : current);
        setAncestors(current => current.map(update));
        setReload(current => current + 1);
      } else {
        await api('/conversations/' + target.id, 'DELETE');
        setTasks(current => current.filter(task => !target.ids.includes(task.id)));
        if (selected && target.ids.includes(selected)) {
          setSelected(null); setDetail(null); setAncestors([]); setMessage(''); setConnected(false);
        }
      }
      setManagement(null);
      refreshTasks().catch(cause => setError(cause.message));
    } catch (cause) { setManagementError((cause as Error).message); }
    finally { setBusy(false); }
  }

  function openConversation(id: string | null) {
    setSelected(id); setQuestionJump(null); setMessage(''); setError(''); setPage('research');
  }

  /** 定位只改变阅读位置，加载完整分支并继续以最新消息作为后续提问的父记录。 */
  function locateQuestion(id: string) {
    try {
      const tail = questionBranchTail(tasks, id, selected);
      if (tail !== selected) openConversation(tail);
      else { setError(''); setPage('research'); }
      setQuestionJump({ id, request: ++jumpSequence.current });
    } catch (cause) { setError((cause as Error).message); }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">研</div><div><strong>观研</strong><span>对话与研究助手</span></div></div>
      <button className="new-chat" disabled={busy} onClick={() => openConversation(null)}>＋ 新对话</button>
      <nav><button className={page === 'research' ? 'nav selected' : 'nav'} onClick={() => setPage('research')}>◈ 对话</button><button className={page === 'models' ? 'nav selected' : 'nav'} onClick={() => setPage('models')}>⚙ 模型连接</button><button className={page === 'memory' ? 'nav selected' : 'nav'} onClick={() => setPage('memory')}>◇ 长期记忆</button></nav>
      <div className="history-heading">最近对话</div>
      <div className="history">{!tasks.length && <p className="sidebar-muted">你的对话会保存在这里。</p>}{conversations.map(group => <div className="history-group" key={group.root}>
        <button disabled={busy} className={'history-item ' + (group.turns.some(task => task.id === selected) ? 'selected' : '')} onClick={() => openConversation(group.turns[0].id)}>
          <strong>{group.turns[0].title || group.turns[0].company}</strong><span className="history-question">{group.turns[0].question || '查看对话'}</span><div>{group.turns[0].status !== 'completed' && <span className={active(group.turns[0].status) ? 'live' : ''}>{statusLabels[group.turns[0].status]}</span>}<time>{time(group.turns[0].updated_at).split(' ')[0]}</time></div>
        </button>
        <ConversationMenu title={group.turns[0].title || group.turns[0].company} disabled={busy} questionCount={group.turns.length} onQuestions={() => setQuestionRoot(group.root)} onAction={action => {
          setManagementError('');
          setManagement({ action, id: group.root, title: group.turns[0].title, ids: group.turns.map(task => task.id) });
        }} />
      </div>)}</div>
      <div className="sidebar-footer"><span className="local-dot" />本机工作空间<span>对话、研究记录与原文保存在本地</span></div>
    </aside>
    <main className={page === 'research' ? 'chat-main' : 'settings-main'}>
      <header className="page-heading"><div><span className="eyebrow">GUANYAN / RESEARCH</span><h1>{page === 'models' ? '模型连接' : page === 'memory' ? '长期记忆' : selectedTitle || '新对话'}</h1><p>{page === 'models' ? '配置云端模型与研究预算。' : page === 'memory' ? '管理个人偏好、投资原则与公司研究积累。' : '直接交流，或让模型调用工具完成研究。'}</p></div></header>
      {error && <div className="error page-error" role="alert">{error}<button aria-label="关闭错误提示" onClick={() => setError('')}>×</button></div>}
      {page === 'models' ? <ModelSettings models={models} onSaved={refreshModels} /> : page === 'memory' ? <MemoryManager /> : <>
        <Conversation thread={detail ? [...ancestors, detail] : []} loading={!!selected && !detail} connected={connected} jump={questionJump} />
        <Composer message={message} onMessage={setMessage} models={models} modelId={modelId} onModel={setModelId} detail={detail} selected={!!selected} busy={busy} onSend={send} onAction={action} onSettings={() => setPage('models')} />
      </>}
    </main>
    {questionGroup && <QuestionHistory title={questionGroup.turns[0].title || questionGroup.turns[0].company} turns={questionGroup.turns} selectedId={questionJump?.id || selected} disabled={busy} onSelect={locateQuestion} onClose={() => setQuestionRoot(null)} />}
    {management && <ConversationDialog key={management.action + management.id} action={management.action} title={management.title} busy={busy} error={managementError} onSubmit={manageConversation} onClose={() => setManagement(null)} />}
  </div>;
}

/** 同一研究的提问归组，保留所有分支记录入口。 */
function groupConversations(tasks: TaskSummary[]) {
  const byId = new Map(tasks.map(task => [task.id, task]));
  const groups = new Map<string, TaskSummary[]>();
  for (const task of tasks) {
    let root = task.id;
    let current = task;
    const visited = new Set([root]);
    while (current.parent_id && !visited.has(current.parent_id)) {
      root = current.parent_id; visited.add(root);
      const parent = byId.get(root);
      if (!parent) break;
      current = parent;
    }
    const turns = groups.get(root) || [];
    turns.push(task); groups.set(root, turns);
  }
  return [...groups].map(([root, turns]) => ({ root, turns: turns.sort((a, b) => b.created_at.localeCompare(a.created_at)) }))
    .sort((a, b) => b.turns[0].updated_at.localeCompare(a.turns[0].updated_at));
}
