import React from 'react';

export type BadgeVariant = 'primary' | 'success' | 'warning' | 'danger' | 'purple' | 'neutral' | 'cyan';

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  size?: 'sm' | 'md';
  dot?: boolean;
  className?: string;
}

export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = 'neutral',
  size = 'md',
  dot = false,
  className = '',
}) => {
  const getColors = (): { bg: string; text: string; border: string; dotColor: string } => {
    switch (variant) {
      case 'primary':
        return {
          bg: 'var(--accent-primary-light)',
          text: 'var(--accent-primary)',
          border: 'rgba(59, 130, 246, 0.3)',
          dotColor: 'var(--accent-primary)',
        };
      case 'success':
        return {
          bg: 'var(--accent-emerald-light)',
          text: 'var(--accent-emerald)',
          border: 'rgba(16, 185, 129, 0.3)',
          dotColor: 'var(--accent-emerald)',
        };
      case 'warning':
        return {
          bg: 'var(--accent-amber-light)',
          text: 'var(--accent-amber)',
          border: 'rgba(245, 158, 11, 0.3)',
          dotColor: 'var(--accent-amber)',
        };
      case 'danger':
        return {
          bg: 'var(--accent-rose-light)',
          text: 'var(--accent-rose)',
          border: 'rgba(244, 63, 94, 0.3)',
          dotColor: 'var(--accent-rose)',
        };
      case 'purple':
        return {
          bg: 'var(--accent-purple-light)',
          text: 'var(--accent-purple)',
          border: 'rgba(168, 85, 247, 0.3)',
          dotColor: 'var(--accent-purple)',
        };
      case 'cyan':
        return {
          bg: 'var(--accent-cyan-light)',
          text: 'var(--accent-cyan)',
          border: 'rgba(6, 182, 212, 0.3)',
          dotColor: 'var(--accent-cyan)',
        };
      default:
        return {
          bg: 'var(--surface-glass-hover)',
          text: 'var(--text-secondary)',
          border: 'var(--border-subtle)',
          dotColor: 'var(--text-muted)',
        };
    }
  };

  const { bg, text, border, dotColor } = getColors();

  return (
    <span
      className={`status-badge ${className}`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: size === 'sm' ? '2px 8px' : '4px 10px',
        fontSize: size === 'sm' ? '0.75rem' : '0.8125rem',
        fontWeight: 600,
        borderRadius: 'var(--radius-full)',
        background: bg,
        color: text,
        border: `1px solid ${border}`,
        lineHeight: 1.2,
        letterSpacing: '0.01em',
      }}
    >
      {dot && (
        <span
          style={{
            width: '6px',
            height: '6px',
            borderRadius: '50%',
            backgroundColor: dotColor,
          }}
        />
      )}
      {children}
    </span>
  );
};

