/** 输入框内的模型选择器，保留每条新消息独立选择连接的行为。 */
import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ModelConnection } from './api';

interface Props {
  models: ModelConnection[];
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}

interface MenuPosition {
  left: number;
  top: number;
  width: number;
  height: number;
}

export default function ModelPicker({ models, value, disabled, onChange }: Props) {
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const options = useRef<HTMLDivElement>(null);
  const listId = useId();
  const selected = models.find(model => model.id === value);
  const [position, setPosition] = useState<MenuPosition | null>(null);
  const [focusedIndex, setFocusedIndex] = useState(0);

  useEffect(() => {
    if (!position) return;

    /** 点击外部或滚动页面时关闭，列表自身滚动保持展开。 */
    function dismiss(event: PointerEvent) {
      if (event.target instanceof Node && !trigger.current?.contains(event.target) && !panel.current?.contains(event.target)) setPosition(null);
    }
    function onScroll(event: Event) {
      if (!(event.target instanceof Node) || !panel.current?.contains(event.target)) setPosition(null);
    }
    function close() { setPosition(null); }
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', close);
    };
  }, [position]);

  useEffect(() => { if (disabled || !models.length) setPosition(null); }, [disabled, models.length]);

  useEffect(() => {
    if (!position) return;
    const list = options.current;
    const item = list?.children[focusedIndex];
    if (!list || !(item instanceof HTMLElement)) return;
    const bounds = item.getBoundingClientRect();
    const viewport = list.getBoundingClientRect();
    if (bounds.top < viewport.top) list.scrollTop -= viewport.top - bounds.top;
    else if (bounds.bottom > viewport.bottom) list.scrollTop += bounds.bottom - viewport.bottom;
  }, [position, focusedIndex]);

  /** 优先向上展开，空间不足时翻转，并限制菜单在视口内。 */
  function open() {
    const bounds = trigger.current?.getBoundingClientRect();
    if (!bounds || disabled || !models.length) return;
    const width = Math.min(320, window.innerWidth - 32);
    const above = bounds.top - 16;
    const below = window.innerHeight - bounds.bottom - 16;
    const upward = above >= Math.min(174, 58 * models.length + 58) || above >= below;
    const height = Math.min(340, 58 * models.length + 58, Math.max(58, upward ? above : below));
    setFocusedIndex(Math.max(0, models.findIndex(model => model.id === value)));
    setPosition({
      width, height,
      left: Math.max(16, Math.min(bounds.left, window.innerWidth - width - 16)),
      top: upward ? Math.max(8, bounds.top - height - 8) : bounds.bottom + 8,
    });
  }

  function choose(model: ModelConnection) {
    if (disabled) return;
    onChange(model.id); setPosition(null); trigger.current?.focus();
  }

  /** 焦点保留在触发按钮，用方向键浏览，Enter 选择，Tab 或 Esc 关闭。 */
  function navigate(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'Escape' || event.key === 'Tab') {
      if (event.key === 'Escape' && position) { event.preventDefault(); event.stopPropagation(); }
      setPosition(null); return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      if (!position) { open(); return; }
      setFocusedIndex(current => event.key === 'Home' ? 0 : event.key === 'End' ? models.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + models.length) % models.length);
    } else if (position && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      const model = models[focusedIndex];
      if (model) choose(model);
    }
  }

  return <div className="model-picker">
    <button ref={trigger} type="button" className="model-picker-trigger" role="combobox" aria-label={'本条消息使用的模型：' + (selected?.name || '未选择')} aria-haspopup="listbox" aria-expanded={!!position} aria-controls={position ? listId : undefined} aria-activedescendant={position ? listId + '-' + focusedIndex : undefined} disabled={disabled || !models.length} onKeyDown={navigate} onClick={() => position ? setPosition(null) : open()}>
      <svg className="model-picker-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z" /><path d="m4 7.5 8 4.5 8-4.5M12 12v9" /></svg>
      <span className="model-picker-value" key={value}>{selected?.name || '选择模型'}</span>
      <svg className="model-picker-chevron" width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="m5 12 5-5 5 5" /></svg>
    </button>
    {position && createPortal(<div ref={panel} className="model-picker-panel" style={position}>
      <div className="model-picker-heading"><strong>选择模型</strong><span>用于下一条消息</span></div>
      <div ref={options} className="model-picker-options" role="listbox" id={listId} aria-label="可用模型">
        {models.map((model, index) => <button key={model.id} id={listId + '-' + index} type="button" role="option" aria-selected={model.id === value} tabIndex={-1} className={'model-picker-option' + (index === focusedIndex ? ' focused' : '')} onPointerMove={() => setFocusedIndex(index)} onClick={() => choose(model)}>
          <span className="model-picker-option-text"><strong>{model.name}</strong><span>{model.model}</span></span>
          {model.id === value && <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m4 10 4 4 8-8" /></svg>}
        </button>)}
      </div>
    </div>, document.body)}
  </div>;
}
