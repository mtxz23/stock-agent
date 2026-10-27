/** 在对话卡片右上角提供菜单，浮层避开侧栏滚动裁剪。 */
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

interface Props {
  title: string;
  disabled: boolean;
  questionCount: number;
  onQuestions: () => void;
  onAction: (action: 'rename' | 'delete') => void;
}

export default function ConversationMenu({ title, disabled, questionCount, onQuestions, onAction }: Props) {
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

  useEffect(() => {
    if (!position) return;
    menu.current?.querySelector('button')?.focus();

    /** 点击菜单和触发按钮以外的位置时收起。 */
    function dismiss(event: PointerEvent) {
      if (event.target instanceof Node && !trigger.current?.contains(event.target) && !menu.current?.contains(event.target)) setPosition(null);
    }
    function close() { setPosition(null); }
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [position]);

  useEffect(() => { if (disabled) setPosition(null); }, [disabled]);

  /** 根据卡片右上角的位置展开，靠近窗口底部时向上弹出。 */
  function toggle() {
    if (position) { setPosition(null); return; }
    const bounds = trigger.current?.getBoundingClientRect();
    if (!bounds) return;
    const height = questionCount > 1 ? 136 : 96;
    setPosition({
      left: Math.max(8, Math.min(bounds.right - 144, window.innerWidth - 152)),
      top: bounds.bottom + height <= window.innerHeight ? bounds.bottom + 4 : Math.max(8, bounds.top - height),
    });
  }

  /** 支持 Esc 返回触发按钮，以及上下方向键切换操作。 */
  function navigate(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation(); setPosition(null); trigger.current?.focus();
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const buttons = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button') || []);
    const current = buttons.findIndex(button => button === document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
      : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  }

  function choose(action: 'rename' | 'delete') {
    setPosition(null); trigger.current?.focus(); onAction(action);
  }

  return <>
    <button ref={trigger} type="button" className="history-menu-trigger" disabled={disabled} title="对话操作" aria-label={'对话操作：' + title} aria-haspopup="menu" aria-expanded={!!position} onClick={toggle}>⋯</button>
    {position && createPortal(<div ref={menu} role="menu" aria-label={'对话操作：' + title} className="history-menu" style={position} onKeyDown={navigate}>
      {questionCount > 1 && <button type="button" role="menuitem" disabled={disabled} onClick={() => { setPosition(null); trigger.current?.focus(); onQuestions(); }}>查看提问 <span className="history-menu-count">{questionCount}</span></button>}
      <button type="button" role="menuitem" disabled={disabled} onClick={() => choose('rename')}>重命名</button>
      <button type="button" role="menuitem" className="delete-conversation" disabled={disabled} onClick={() => choose('delete')}>删除</button>
    </div>, document.body)}
  </>;
}
