/** 按提问与回复显示研究对话，保留可展开的进展与原文。 */
import { useEffect, useRef } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import MemoryUsage from './MemoryUsage';
import { active, statusLabels, time, type TaskDetail } from './api';

export default function Conversation({ thread, loading, connected, jump }: {
  thread: TaskDetail[]; loading: boolean; connected: boolean; jump?: { id: string; request: number } | null;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const lastJump = useRef(0);
  const latest = thread.at(-1);
  useEffect(() => { following.current = true; }, [latest?.task.id, jump?.request]);
  useEffect(() => {
    const box = scroll.current;
    if (!box || loading) return;
    if (jump && jump.request !== lastJump.current) {
      const question = document.getElementById('question-' + jump.id);
      if (question && box.contains(question)) {
        following.current = false;
        lastJump.current = jump.request;
        // 只滚动消息容器，避免连带移动侧栏或输入框。
        box.scrollTop += question.getBoundingClientRect().top - box.getBoundingClientRect().top - 18;
        question.focus({ preventScroll: true });
        return;
      }
    }
    if (following.current) box.scrollTop = box.scrollHeight;
  }, [thread, loading, jump]);

  return <div className="conversation-scroll" ref={scroll} onScroll={() => {
    const box = scroll.current;
    if (box) following.current = box.scrollHeight - box.scrollTop - box.clientHeight < 100;
  }}>
    {loading ? <div className="loading"><span className="spinner" />正在恢复研究对话…</div> : !thread.length ? <div className="chat-welcome">
      <span className="welcome-mark">研</span><span className="eyebrow">从一个问题开始</span><h2>有什么想聊的？</h2>
      <p>可以直接交流、解释概念，也可以让我研究公司。<br />例如：帮我研究阿里巴巴的经营质量和主要风险。</p>
      <div className="welcome-examples"><span>日常交流</span><span>知识与思路</span><span>公司研究</span></div>
    </div> : <div className="conversation-messages">{thread.map((detail, index) => <ResearchTurn key={detail.task.id} detail={detail} current={index === thread.length - 1} connected={connected} targeted={detail.task.id === jump?.id} />)}</div>}
  </div>;
}

/** 每条提问对应一份研究回复，来源与进展归属保持明确。 */
function ResearchTurn({ detail, current, connected, targeted }: { detail: TaskDetail; current: boolean; connected: boolean; targeted: boolean }) {
  const { task, events, sources } = detail;
  const running = active(task.status);
  const lastOutput = [...events].reverse().find(event => event.kind === 'model_message' && typeof event.data.text === 'string');
  const latestEvent = events.at(-1);
  const draft = typeof lastOutput?.data.text === 'string' ? lastOutput.data.text : '';
  const content = task.report || (running ? draft : '');

  return <section className="research-turn" aria-label={task.parent_id ? '后续消息' : '首次消息'}>
    <article id={'question-' + task.id} tabIndex={-1} className={'message user-message' + (targeted ? ' question-target' : '')}>
      <div className="message-label"><span>你</span><time>{time(task.created_at)}</time></div>
      <div className="user-bubble">{!task.parent_id && task.company && <span className="company-context">研究对象 · {task.company}</span>}<p>{task.question || '请研究这家公司的业务、经营质量和主要不确定性。'}</p></div>
    </article>
    <article className="message assistant-message">
      <div className="assistant-avatar" aria-hidden="true">研</div>
      <div className="assistant-body">
        <div className="message-label"><strong>观研</strong><span>{task.model_snapshot.name}</span><span className={'badge ' + task.status}>{statusLabels[task.status]}</span></div>
        {running && <div className="research-status" role="status"><span className="spinner" /><span>{latestEvent?.message || '正在准备回复…'}</span>{current && <small>{connected ? '进展已连接' : '正在连接进展…'}</small>}</div>}
        {task.error && <div className="error" role="alert">{task.error}</div>}
        {content ? <div className="markdown answer"><Markdown remarkPlugins={[remarkGfm]} components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{content}</Markdown>{!task.report && <p className="draft-note">处理中间输出，最终回复尚未生成。</p>}</div> : !running && <p className="muted">{task.status === 'paused' || task.status === 'interrupted' ? '回复已停止，可继续处理或发送新消息。' : task.status === 'failed' ? '这次回复未能完成，可查看过程与错误后重试。' : '尚未生成回复。'}</p>}
        <MemoryUsage events={events} />
        <details className="research-details">
          <summary><span>◈ 处理过程</span><span>{events.length} 条记录</span></summary>
          <div className="timeline">{events.map(event => <article className={'event event-' + event.kind} key={event.id}>
            <span className="event-dot" /><div className="event-body"><div className="event-heading"><strong>{event.message}</strong><time>{time(event.created_at)}</time></div>
              {event.kind === 'history_recalled' ? <HistoryRecall data={event.data} /> : typeof event.data.text === 'string' ? <div className="markdown"><Markdown remarkPlugins={[remarkGfm]}>{event.data.text}</Markdown></div> : typeof event.data.summary === 'string' ? <details><summary>查看对话摘要</summary><div className="markdown"><Markdown remarkPlugins={[remarkGfm]}>{event.data.summary}</Markdown></div></details> : Object.keys(event.data).length > 0 && <details><summary>{event.kind === 'tool_finished' ? '查看工具结果' : event.kind === 'tool_started' ? '查看调用参数' : '查看详情'}</summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details>}
            </div>
          </article>)}</div>
        </details>
        {(sources.length > 0 || task.kind === 'research') && <details className="research-details">
          <summary><span>↗ 原文来源</span><span>{sources.length} 份已归档</span></summary>
          <div className="sources">{!sources.length && <p className="muted small">尚未成功读取原文，搜索摘要只作为线索。</p>}{sources.map(source => <article className="source" key={source.id}>
            <div className="source-top"><span className="source-id mono">{source.id}</span><span className="muted small">{source.media_type.includes('pdf') ? 'PDF' : '网页'} · {time(source.fetched_at)}</span></div>
            <h3>{source.title}</h3><a className="break" href={source.url} target="_blank" rel="noreferrer">{source.url}</a>
            <div className="source-bottom"><span className="mono small muted" title={source.content_hash}>SHA-256 {source.content_hash.slice(0, 16)}…</span><a href={'/api/sources/' + source.id + '/download'}>下载归档原文 ↓</a></div>
          </article>)}</div>
        </details>}
      </div>
    </article>
  </section>;
}


interface RecallFragment {
  task_id: string; title: string; created_at: string; section: string;
  text: string; score: number; start: number; end: number;
}

/** 自动召回片段保留时间、相似度和原文编号，方便核查。 */
function HistoryRecall({ data }: { data: Record<string, unknown> }) {
  const items = (Array.isArray(data.items) ? data.items : []) as RecallFragment[];
  return <details>
    <summary>{items.length ? '查看自动找回的历史' : '查看检索条件'}</summary>
    {items.map(item => <div className="markdown" key={item.task_id + item.section + item.start}>
      <strong>{item.title || '历史对话'}</strong>
      <small>{time(item.created_at)} · {item.section === 'report' ? '历史回复' : '历史提问'} · 相似度 {item.score.toFixed(2)}</small>
      <Markdown remarkPlugins={[remarkGfm]}>{item.text}</Markdown>
      <small className="mono break">记录 {item.task_id} · 原文位置 {item.start}–{item.end}</small>
    </div>)}
    <details><summary>检索依据</summary><pre>{String(data.query || '')}</pre></details>
  </details>;
}
