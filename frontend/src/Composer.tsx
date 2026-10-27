/** 统一消息输入框，支持日常聊天、工具研究和每轮模型选择。 */
import { useRef } from 'react';
import { active, type ModelConnection, type TaskDetail } from './api';
import ModelPicker from './ModelPicker';

export default function Composer({ message, onMessage, models, modelId, onModel, detail, selected, busy, onSend, onAction, onSettings }: {
  message: string; onMessage: (value: string) => void;
  models: ModelConnection[]; modelId: string; onModel: (value: string) => void;
  detail: TaskDetail | null; selected: boolean; busy: boolean;
  onSend: () => Promise<void>; onAction: (name: 'pause' | 'resume') => Promise<void>; onSettings: () => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const ready = models.some(model => model.has_api_key);
  const status = detail?.task.status || '';
  const running = active(status);
  const resumable = ['paused', 'interrupted', 'failed'].includes(status);
  const canSend = ready && !!modelId && !!message.trim() && !busy && !running && (!selected || !!detail);

  /** 点击边缘或底部空白时聚焦输入，不干扰按钮和模型菜单。 */
  function focusInput(event: React.MouseEvent<HTMLFormElement>) {
    if (event.target instanceof Element && event.target.closest('button, textarea, input, select, a')) return;
    input.current?.focus();
  }

  /** 输入法选字不提交，Enter 发送，Shift+Enter 换行。 */
  function keyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (canSend) void onSend();
    }
  }

  return <div className="composer-area">
    {!ready ? <div className="setup-notice"><div><strong>{models.length ? '补充模型密钥后开始对话' : '连接一个云端模型，开始对话'}</strong><p>在模型连接中保存 API 地址、模型名称和密钥。</p></div><button className="primary" onClick={onSettings}>配置模型 ↗</button></div> : <form className="composer" onClick={focusInput} onSubmit={event => { event.preventDefault(); if (canSend) void onSend(); }}>
      <textarea ref={input} aria-label="消息" maxLength={4000} rows={3} value={message} onChange={event => onMessage(event.target.value)} onKeyDown={keyDown} disabled={busy || (selected && !detail)} placeholder="发送消息..." />
      <div className="composer-footer">
        <ModelPicker models={models.filter(model => model.has_api_key)} value={modelId} onChange={onModel} disabled={busy} />
        <div className="composer-actions">
          {running && <button type="button" disabled={busy} onClick={() => void onAction('pause')}>停止回复</button>}
          {resumable && <button type="button" disabled={busy} onClick={() => void onAction('resume')}>继续上条回复</button>}
          <button type="submit" className="primary send-button" disabled={!canSend} aria-label="发送消息">{busy ? '提交中…' : '发送 ↑'}</button>
        </div>
      </div>
    </form>}
  </div>;
}
