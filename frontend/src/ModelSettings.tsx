/** 管理云端模型连接，密钥只能填写替换，不能读取。 */
import { useState } from 'react';
import EmbeddingSettings from './EmbeddingSettings';
import { api, type ModelConnection, type ModelForm } from './api';

const blank: ModelForm = { name: '', protocol: 'chat_completions', base_url: '', model: '', api_key: '', max_tokens: 4096, max_steps: 12, context_budget: 64000, token_parameter: 'max_tokens' };

export default function ModelSettings({ models, onSaved }: { models: ModelConnection[]; onSaved: () => Promise<void> }) {
  const [form, setForm] = useState<ModelForm>(blank);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  /** 只复制公开配置，已保存密钥始终留空。 */
  function edit(model: ModelConnection) {
    setEditing(model.id);
    const { name, protocol, base_url, model: modelName, max_tokens, max_steps, context_budget, token_parameter } = model;
    setForm({ name, protocol, base_url, model: modelName, max_tokens, max_steps, context_budget: context_budget || 64000, token_parameter, api_key: '' });
    setError(''); setNotice('');
  }

  /** 保存或测试时统一显示失败原因。 */
  async function save(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      const result = await api<ModelConnection>(editing ? `/models/${editing}` : '/models', editing ? 'PUT' : 'POST', form);
      setEditing(result.id); setForm({ ...form, api_key: '' });
      await onSaved(); setNotice(result.has_api_key ? '连接已保存。建议运行连接测试，检查工具调用兼容性。' : '基础配置已保存，待填写 API Key。填写并保存后即可测试连接。');
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  async function test(id: string) {
    setBusy(true); setError(''); setNotice('');
    try { const result = await api<{message: string}>(`/models/${id}/test`, 'POST'); setNotice(result.message); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  return <div className="settings-grid">
    <section className="panel">
      <div className="section-heading"><div><span className="eyebrow">CLOUD CONNECTION</span><h2>{editing ? '编辑模型连接' : '添加模型连接'}</h2></div></div>
      <p className="muted">使用支持工具调用的 Chat Completions 兼容云端 API。可保存多个连接，在发送消息时选择。</p>
      <form onSubmit={save} className="stack">
        <label>连接名称<input required maxLength={80} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="例如：日常研究模型" /></label>
        <label>API 基础地址<input required type="url" value={form.base_url} onChange={e => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" /><small>填写服务商提供的 HTTPS 地址，系统会拼接 /chat/completions。</small></label>
        <label>模型名称<input required value={form.model} onChange={e => setForm({ ...form, model: e.target.value })} placeholder="填写服务商实际提供的模型 ID" /></label>
        <label>API Key<input type="password" autoComplete="new-password" value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} placeholder={editing && models.find(model => model.id === editing)?.has_api_key ? '留空保留现有密钥' : '填写密钥后保存即可启用'} /><small>可先留空保存基础配置。密钥加密保存在本机，保存后不会回传页面。</small></label>
        <div className="two-columns"><label>单条消息轮数上限<input type="number" min={2} max={30} value={form.max_steps} onChange={e => setForm({ ...form, max_steps: Number(e.target.value) })} /></label><label>单轮输出 token 上限<input type="number" min={256} max={32768} value={form.max_tokens} onChange={e => setForm({ ...form, max_tokens: Number(e.target.value) })} /></label></div>
        <label>本地上下文预算<input type="number" min={8192} max={262144} value={form.context_budget} onChange={e => setForm({ ...form, context_budget: Number(e.target.value) })} /><small>按模型可承受的上下文设置；系统估算输入量、预留输出，较早内容过长时自动压缩。默认 64000，估算不等于服务商精确 token 数。</small></label>
        <label>输出预算参数<select value={form.token_parameter} onChange={e => setForm({ ...form, token_parameter: e.target.value as ModelForm['token_parameter'] })}><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></select><small>按服务商接口要求选择；部分模型使用 max_completion_tokens。</small></label>
        <p className="muted small">轮数限制模型请求次数；一轮可以调用多个工具。上下文、工具结果和报告会发送至你配置的服务商。</p>
        {error && <div role="alert" className="error">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}
        <div className="actions"><button className="primary" disabled={busy}>{busy ? '处理中…' : '保存连接'}</button>{editing && <button type="button" disabled={busy || !models.find(model => model.id === editing)?.has_api_key} onClick={() => test(editing)}>测试已保存连接</button>}</div>
      </form>
    </section>
    <section className="panel connections"><div className="section-heading"><h2>已保存连接</h2><button onClick={() => { setEditing(null); setForm(blank); setError(''); setNotice(''); }}>＋ 新增</button></div>
      {!models.length && <p className="muted">还没有模型连接。保存后即可开始研究。</p>}
      {models.map(model => <div className={`connection ${editing === model.id ? 'selected' : ''}`} key={model.id}>
        <strong>{model.name}</strong><span className={model.has_api_key ? 'small live' : 'small muted'}>{model.has_api_key ? '已填写密钥 · 可测试' : '待填写密钥'}</span><span className="mono small">{model.model}</span><span className="muted small break">{model.base_url}</span>
        <div className="actions"><button onClick={() => edit(model)}>编辑</button><button disabled={busy || !model.has_api_key} onClick={() => test(model.id)}>测试连接</button></div>
      </div>)}
      <div className="hint"><strong>配置与任务独立保存</strong><p>新配置用于新任务。已有任务继续使用创建时的配置，研究过程保持一致。</p></div>
    </section>
    <EmbeddingSettings models={models} />
  </div>;
}
