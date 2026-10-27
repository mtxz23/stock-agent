/** 独立配置历史语义检索，复用已有API连接的密钥。 */
import { useEffect, useState } from 'react';
import { api, type ModelConnection } from './api';

interface EmbeddingConfig { connection_id: string; model: string; dimensions: number; min_score: number }
interface ConfigStatus { config: EmbeddingConfig | null; indexed_tasks: number; database: string }
const initial: EmbeddingConfig = { connection_id: '', model: 'Qwen/Qwen3-Embedding-8B', dimensions: 1024, min_score: 0.3 };

export default function EmbeddingSettings({ models }: { models: ModelConnection[] }) {
  const [form, setForm] = useState(initial);
  const [saved, setSaved] = useState(false);
  const [count, setCount] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const available = models.filter(model => model.has_api_key);

  useEffect(() => {
    let disposed = false;
    api<ConfigStatus>('/embeddings/config').then(result => {
      if (!disposed) {
        if (result.config) { setForm(result.config); setSaved(true); }
        setCount(result.indexed_tasks);
      }
    }).catch(cause => { if (!disposed) setError((cause as Error).message); });
    return () => { disposed = true; };
  }, [models]);

  /** 更改后先保存，测试始终使用已保存的配置。 */
  function change(values: Partial<EmbeddingConfig>) { setForm({ ...form, ...values }); setSaved(false); setNotice(''); }

  async function execute(action: 'save' | 'test') {
    setBusy(true); setError(''); setNotice('');
    try {
      if (action === 'save') {
        const result = await api<ConfigStatus>('/embeddings/config', 'PUT', form);
        setSaved(true); setCount(result.indexed_tasks); setNotice('检索配置已保存。首次检索时会为已有对话生成向量。');
      } else {
        const result = await api<{ message: string }>('/embeddings/test', 'POST');
        setNotice(result.message);
      }
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="panel embedding-settings">
    <div className="section-heading"><h2>历史向量检索</h2><span className="muted small">Qdrant · 已索引 {count} 条记录</span></div>
    <p className="muted">新问题首次分析前自动找回相关历史，模型也可通过工具补查。检索模型与聊天模型独立配置。</p>
    <form className="stack" onSubmit={event => { event.preventDefault(); void execute('save'); }}>
      <div className="two-columns">
        <label>API 连接<select required value={form.connection_id} disabled={busy} onChange={event => change({ connection_id: event.target.value })}>
          <option value="">选择已填写密钥的连接</option>{available.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
        </select><small>复用该连接的地址和密钥，请确认服务商支持下面的 embedding 模型。</small></label>
        <label>Embedding 模型<input required list="embedding-models" value={form.model} disabled={busy} onChange={event => change({ model: event.target.value })} />
          <datalist id="embedding-models"><option value="Qwen/Qwen3-Embedding-8B" /><option value="Qwen/Qwen3-VL-Embedding-8B" /></datalist>
          <small>当前检索的是文字对话，推荐 Qwen3-Embedding-8B。</small></label>
      </div>
      <div className="two-columns">
        <label>向量维度<select value={form.dimensions} disabled={busy} onChange={event => change({ dimensions: Number(event.target.value) })}>
          {[64, 128, 256, 512, 768, 1024, 1536, 2048, 2560, 4096].map(size => <option key={size}>{size}</option>)}
        </select><small>默认 1024；更换模型或维度会使用独立索引。</small></label>
        <label>最低相似度<input type="number" min={-1} max={1} step={0.05} value={form.min_score} disabled={busy} onChange={event => change({ min_score: Number(event.target.value) })} /><small>默认 0.3；越高越严格，分数不代表结论可信度。</small></label>
      </div>
      <p className="muted small">新增或变化的历史原文会发送到选定服务商生成向量。测试向量接口也会产生 API 调用。</p>
      {error && <div role="alert" className="error">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}
      <div className="actions"><button className="primary" disabled={busy || !available.length}>{busy ? '处理中…' : '保存检索配置'}</button><button type="button" disabled={busy || !saved} onClick={() => void execute('test')}>测试向量接口</button></div>
    </form>
  </section>;
}
