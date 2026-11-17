import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useTheme } from '../../context/ThemeContext';
import {
  DashboardIcon,
  MessageSquareIcon,
  TicketIcon,
  AlertTriangleIcon,
  FileTextIcon,
  DatabaseIcon,
  BarChart3Icon,
  SettingsIcon,
} from '../common/Icons';

export type NimbusFx = 'plane' | 'drop' | 'spin' | 'glow' | 'wave' | 'pop' | 'none';

export type LiquidNavItem = {
  label: string;
  to?: string;
  href?: string;
  icon?: React.ReactNode;
  fx?: NimbusFx;
  badge?: string;
};

export type LiquidNavbarProps = {
  items?: LiquidNavItem[];
  defaultActive?: number;
  active?: number;
  onChange?: (index: number, item: LiquidNavItem) => void;
  tone?: 'light' | 'dark';
  compact?: boolean;
  sound?: boolean;
  className?: string;
  style?: React.CSSProperties;
};

export const DEFAULT_PLATFORM_ITEMS: LiquidNavItem[] = [
  { label: 'Overview', to: '/app', icon: <DashboardIcon size={18} />, fx: 'glow' },
  { label: 'Conversations', to: '/conversations', icon: <MessageSquareIcon size={18} />, fx: 'pop', badge: 'Live' },
  { label: 'Tickets', to: '/tickets', icon: <TicketIcon size={18} />, fx: 'drop' },
  { label: 'Escalations', to: '/escalations', icon: <AlertTriangleIcon size={18} />, fx: 'wave' },
  { label: 'Evidence', to: '/evidence', icon: <FileTextIcon size={18} />, fx: 'plane' },
  { label: 'Knowledge', to: '/knowledge', icon: <DatabaseIcon size={18} />, fx: 'spin' },
  { label: 'Analytics', to: '/analytics', icon: <BarChart3Icon size={18} />, fx: 'glow' },
  { label: 'Settings', to: '/settings', icon: <SettingsIcon size={18} />, fx: 'wave' },
];

/* ---------- styles ---------- */
const NIMBUS_CSS = `
.nimbus-nav-container {
  position: fixed;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 950;
  pointer-events: auto;
  max-width: calc(100vw - 32px);
}
.nimbus-nav {
  --n-tint: rgba(255, 255, 255, 0.14);
  --n-fg: #0B132B;
  --n-fg-soft: rgba(11, 19, 43, 0.65);
  --n-rim: rgba(255, 255, 255, 0.75);
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  isolation: isolate;
  font-family: ui-sans-serif, -apple-system, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  user-select: none;
  -webkit-tap-highlight-color: transparent;
}
.nimbus-nav[data-tone="dark"] {
  --n-tint: rgba(15, 23, 42, 0.78);
  --n-fg: #FFFFFF;
  --n-fg-soft: rgba(226, 232, 240, 0.8);
  --n-rim: rgba(255, 255, 255, 0.22);
}
.nimbus-glass {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  z-index: -2;
  overflow: hidden;
  background: var(--n-tint);
  -webkit-backdrop-filter: blur(24px) saturate(1.6) brightness(1.06);
  backdrop-filter: blur(24px) saturate(1.6) brightness(1.06);
  box-shadow: inset 0 1px 1px var(--n-rim),
              inset 0 0 0 1px rgba(255, 255, 255, 0.12),
              0 16px 40px -10px rgba(0, 15, 40, 0.6),
              0 2px 8px rgba(0, 15, 40, 0.2);
}
.nimbus-drop {
  display: block;
  position: absolute;
  top: 6px;
  bottom: 6px;
  left: 0;
  border-radius: 999px;
  z-index: 1;
  pointer-events: none;
  will-change: transform, width;
  transition: transform 0.34s cubic-bezier(0.16, 1, 0.3, 1), width 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  background: radial-gradient(120% 140% at 30% 0%, rgba(255, 255, 255, 0.24), rgba(255, 255, 255, 0.02) 60%), rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.22);
  box-shadow: inset 0 1px 1px 0 rgba(255, 255, 255, 0.7),
              inset 0 -1px 2px 0 rgba(6, 182, 212, 0.25),
              0 4px 18px -2px rgba(6, 182, 212, 0.28);
}
.nimbus-nav[data-tone="light"] .nimbus-drop {
  background: radial-gradient(120% 140% at 30% 0%, rgba(255, 255, 255, 0.95), rgba(255, 255, 255, 0.5) 60%), rgba(255, 255, 255, 0.65);
  border: 1px solid rgba(255, 255, 255, 0.85);
  box-shadow: inset 0 1px 1.5px 0 #ffffff,
              inset 0 -1px 2px 0 rgba(0, 162, 255, 0.2),
              0 4px 14px -2px rgba(0, 100, 220, 0.18);
}
.nimbus-item {
  position: relative;
  z-index: 2;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 38px;
  padding: 0 12px;
  border: 0;
  border-radius: 999px;
  background: none;
  color: var(--n-fg-soft);
  font-family: inherit;
  font-weight: 550;
  font-size: 12.5px;
  line-height: 1;
  letter-spacing: -0.01em;
  text-decoration: none;
  cursor: pointer;
  transition: color 0.2s ease, transform 0.2s ease;
  flex-shrink: 0;
}
.nimbus-item:hover {
  color: var(--n-fg);
}
.nimbus-item[aria-current="page"] {
  font-weight: 650;
  color: #ffffff;
  text-shadow: 0 0 12px rgba(255, 255, 255, 0.4);
}
.nimbus-nav[data-tone="light"] .nimbus-item[aria-current="page"] {
  color: #0f172a;
  text-shadow: none;
}
.nimbus-item:focus-visible {
  outline: 2px solid #00A2FF;
  outline-offset: 2px;
}
.nv-ico {
  position: relative;
  display: inline-flex;
  flex: none;
}
.nv-ico svg {
  transition: transform 0.35s cubic-bezier(0.3, 1.6, 0.5, 1);
}
.nimbus-item[aria-current="page"] .nv-ico svg {
  transform: translateY(-1px) scale(1.08);
}
.nv-badge {
  position: absolute;
  top: -2px;
  right: -4px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #06B6D4;
  box-shadow: 0 0 8px #06B6D4;
}
.nv-ico[data-fx="pop"] svg { animation: nvPop 0.5s cubic-bezier(0.3, 1.6, 0.5, 1) both; }
@keyframes nvPop { 0% { transform: scale(0.72); } 60% { transform: scale(1.2); } 100% { transform: translateY(-1px) scale(1.08); } }
.nv-ico[data-fx="plane"] svg { animation: nvPlane 1.15s cubic-bezier(0.55, 0, 0.3, 1) both; }
@keyframes nvPlane { 0% { transform: none; } 12% { transform: translate(-3px, 2px) rotate(7deg); } 42% { transform: translate(64px, -26px) rotate(-24deg) scale(0.9); opacity: 0; } 43% { transform: translate(-60px, 16px) rotate(-12deg) scale(0.9); opacity: 0; } 58% { opacity: 1; } 80% { transform: translate(3px, -1px) rotate(2deg); } 100% { transform: translateY(-1px) scale(1.08); } }
.nv-ico[data-fx="drop"] svg { animation: nvDrop 0.95s cubic-bezier(0.4, 0, 0.3, 1) both; transform-origin: 50% 100%; }
@keyframes nvDrop { 0% { transform: translateY(-1px); } 26% { transform: translateY(-13px) scale(0.88, 1.12); } 46% { transform: translateY(3px) scale(1.3, 0.68); } 62% { transform: translateY(-3px) scale(0.92, 1.08); } 80% { transform: translateY(0) scale(1.04, 0.97); } 100% { transform: translateY(-1px) scale(1.08); } }
.nv-ico[data-fx="spin"] svg { animation: nvSpin 1s cubic-bezier(0.2, 0.75, 0.25, 1) both; }
@keyframes nvSpin { 0% { transform: perspective(80px) rotateY(0); } 100% { transform: perspective(80px) rotateY(360deg) translateY(-1px) scale(1.08); } }
.nv-ico[data-fx="glow"] svg { animation: nvHop 0.8s cubic-bezier(0.3, 1.4, 0.5, 1) both; transform-origin: 50% 100%; }
@keyframes nvHop { 0% { transform: none; } 30% { transform: translateY(-5px) scale(1.04, 0.96); } 55% { transform: translateY(0) scale(1.12, 0.88); } 75% { transform: translateY(-2px) scale(0.98, 1.04); } 100% { transform: translateY(-1px) scale(1.08); } }
.nv-ico[data-fx="wave"] svg { animation: nvWave 0.9s ease-in-out both; transform-origin: 50% 92%; }
@keyframes nvWave { 0% { transform: none; } 20% { transform: rotate(-15deg); } 40% { transform: rotate(12deg); } 60% { transform: rotate(-8deg); } 80% { transform: rotate(4deg); } 100% { transform: translateY(-1px) scale(1.08); } }
.nimbus-nav[data-compact="true"] .nimbus-item { padding: 0 12px; }
.nimbus-nav[data-compact="true"] .nimbus-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 880px) {
  .nimbus-nav:not([data-compact="false"]) .nimbus-item { padding: 0 11px; }
  .nimbus-nav:not([data-compact="false"]) .nimbus-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
}
@media (prefers-reduced-motion: reduce) {
  .nv-ico svg, .nimbus-drop { animation: none !important; transition: none !important; }
}
`;

export function LiquidNavbar({
  items = DEFAULT_PLATFORM_ITEMS,
  defaultActive = 0,
  active,
  onChange,
  tone,
  compact,
  sound = true,
  className,
  style,
}: LiquidNavbarProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const { isDark } = useTheme();

  // Resolve active index from router if not explicitly controlled
  const routeActiveIndex = React.useMemo(() => {
    const p = location.pathname;
    const idx = items.findIndex((it) => {
      if (!it.to) return false;
      if (it.to === '/app') return p === '/app' || p.startsWith('/app/overview');
      return p.startsWith(it.to);
    });
    return idx >= 0 ? idx : 0;
  }, [location.pathname, items]);

  const [inner, setInner] = React.useState(defaultActive);
  const current = active !== undefined ? active : (location ? routeActiveIndex : inner);

  const effectiveTone = tone || (isDark ? 'dark' : 'light');

  const navRef = React.useRef<HTMLElement>(null);
  const dropRef = React.useRef<HTMLSpanElement>(null);
  const itemRefs = React.useRef<(HTMLElement | null)[]>([]);
  const iconRefs = React.useRef<(HTMLSpanElement | null)[]>([]);
  const audio = React.useRef<AudioContext | null>(null);
  const [fx, setFx] = React.useState({ i: -1, n: 0 });

  /* ---- positioning the active glass droplet ---- */
  const measure = React.useCallback(() => {
    const nav = navRef.current;
    const el = itemRefs.current[current];
    const drop = dropRef.current;
    if (!nav || !el || !drop) return;
    drop.style.transform = `translateX(${el.offsetLeft}px)`;
    drop.style.width = `${el.offsetWidth}px`;
  }, [current]);

  React.useLayoutEffect(() => {
    measure();
  }, [measure, items.length, compact]);

  React.useEffect(() => {
    const ro = new ResizeObserver(() => {
      measure();
    });
    if (navRef.current) ro.observe(navRef.current);
    return () => ro.disconnect();
  }, [measure]);

  /* ---- audio plip ---- */
  const plip = () => {
    if (!sound) return;
    try {
      const AC = window.AudioContext || (window as any).webkitAudioContext;
      const ctx = (audio.current ||= new AC());
      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }
      const t = ctx.currentTime;
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = 'sine';
      o.frequency.setValueAtTime(420 + Math.random() * 80, t);
      o.frequency.exponentialRampToValueAtTime(1500, t + 0.07);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.16, t + 0.008);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
      o.connect(g).connect(ctx.destination);
      o.start(t);
      o.stop(t + 0.18);
    } catch {}
  };

  const select = (i: number) => {
    const item = items[i];
    setFx((f) => ({ i, n: f.n + 1 }));
    plip();
    if (i !== current) {
      if (active === undefined) setInner(i);
      onChange?.(i, item);
      if (item.to) {
        navigate(item.to);
      }
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    e.preventDefault();
    const n = (current + (e.key === 'ArrowRight' ? 1 : -1) + items.length) % items.length;
    select(n);
    itemRefs.current[n]?.focus();
  };

  return (
    <div className="nimbus-nav-container">
      <nav
        ref={navRef as any}
        className={['nimbus-nav', className].filter(Boolean).join(' ')}
        data-tone={effectiveTone}
        data-compact={compact === undefined ? undefined : String(compact)}
        aria-label="Application Command Dock"
        onKeyDown={onKeyDown}
        style={style}
      >
        <style dangerouslySetInnerHTML={{ __html: NIMBUS_CSS }} />
        <span className="nimbus-glass" aria-hidden="true" />
        <span className="nimbus-drop" ref={dropRef} aria-hidden="true" />
        {items.map((it, i) => {
          const kind = it.fx ?? 'pop';
          const playing = fx.i === i && kind !== 'none';
          const isCurrent = i === current;
          const props = {
            ref: (el: HTMLElement | null) => { itemRefs.current[i] = el; },
            className: 'nimbus-item',
            'aria-current': isCurrent ? ('page' as const) : undefined,
            onClick: (e: React.MouseEvent) => {
              e.preventDefault();
              select(i);
            },
          };
          const content = (
            <>
              {it.icon && (
                <span
                  className="nv-ico"
                  key={playing ? fx.n : 0}
                  data-fx={playing ? kind : undefined}
                  ref={(el) => { iconRefs.current[i] = el; }}
                >
                  {it.icon}
                  {it.badge && <span className="nv-badge" title={it.badge} />}
                </span>
              )}
              <span className="nimbus-label">{it.label}</span>
            </>
          );
          return it.to || it.href ? (
            <a key={it.label + i} {...(props as any)} href={it.to || it.href} aria-label={it.label}>
              {content}
            </a>
          ) : (
            <button key={it.label + i} {...(props as any)} type="button" aria-label={it.label}>
              {content}
            </button>
          );
        })}
      </nav>
    </div>
  );
}

export { LiquidNavbar as Component };
export default LiquidNavbar;
