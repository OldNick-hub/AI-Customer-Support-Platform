import React from 'react';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'outline' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  loading?: boolean;
  icon?: React.ReactNode;
  children: React.ReactNode;
}

export const Button: React.FC<ButtonProps> = ({
  variant = 'primary',
  size = 'md',
  loading = false,
  icon,
  children,
  disabled,
  style,
  className = '',
  ...props
}) => {
  const getStyles = (): React.CSSProperties => {
    const base: React.CSSProperties = {
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      gap: '8px',
      fontWeight: 600,
      cursor: disabled || loading ? 'not-allowed' : 'pointer',
      opacity: disabled || loading ? 0.6 : 1,
      borderRadius: 'var(--radius-md)',
      transition: 'all var(--transition-fast)',
      fontFamily: 'var(--font-sans)',
      border: '1px solid transparent',
    };

    // Sizing
    if (size === 'sm') {
      base.padding = '6px 12px';
      base.fontSize = '0.8125rem';
    } else if (size === 'lg') {
      base.padding = '12px 24px';
      base.fontSize = '1rem';
    } else {
      base.padding = '8px 16px';
      base.fontSize = '0.875rem';
    }

    // Variants
    switch (variant) {
      case 'primary':
        base.background = 'var(--accent-primary)';
        base.color = '#ffffff';
        base.boxShadow = 'var(--shadow-sm)';
        break;
      case 'secondary':
        base.background = 'var(--surface-glass-elevated)';
        base.color = 'var(--text-primary)';
        base.border = 'var(--border-card)';
        break;
      case 'outline':
        base.background = 'transparent';
        base.color = 'var(--text-primary)';
        base.border = 'var(--border-card)';
        break;
      case 'danger':
        base.background = 'var(--accent-rose)';
        base.color = '#ffffff';
        break;
      case 'ghost':
        base.background = 'transparent';
        base.color = 'var(--text-secondary)';
        break;
    }

    return base;
  };

  return (
    <button
      className={`glass-btn ${className}`}
      disabled={disabled || loading}
      style={{ ...getStyles(), ...style }}
      {...props}
    >
      {loading ? (
        <span
          style={{
            width: '14px',
            height: '14px',
            border: '2px solid currentColor',
            borderRightColor: 'transparent',
            borderRadius: '50%',
            display: 'inline-block',
            animation: 'spin 0.75s linear infinite',
          }}
        />
      ) : (
        icon
      )}
      {children}
    </button>
  );
};

