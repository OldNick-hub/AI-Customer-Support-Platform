import React from 'react';

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
  variant?: 'base' | 'elevated' | 'highlight';
  glow?: 'none' | 'blue' | 'rose';
  className?: string;
}

export const Card: React.FC<CardProps> = ({
  children,
  variant = 'base',
  glow = 'none',
  className = '',
  style,
  ...props
}) => {
  const getGlowShadow = () => {
    if (glow === 'blue') return 'var(--shadow-glow-blue)';
    if (glow === 'rose') return 'var(--shadow-glow-rose)';
    return variant === 'elevated' ? 'var(--shadow-lg)' : 'var(--shadow-md)';
  };

  const getBackground = () => {
    if (variant === 'elevated') return 'var(--surface-glass-elevated)';
    return 'var(--surface-glass-card)';
  };

  return (
    <div
      className={`glass-card ${className}`}
      style={{
        background: getBackground(),
        backdropFilter: 'var(--blur-md)',
        WebkitBackdropFilter: 'var(--blur-md)',
        border: variant === 'highlight' ? 'var(--border-highlight)' : 'var(--border-card)',
        borderRadius: 'var(--radius-lg)',
        boxShadow: getGlowShadow(),
        padding: '20px',
        transition: 'all var(--transition-normal)',
        ...style,
      }}
      {...props}
    >
      {children}
    </div>
  );
};

