import React from 'react';
import { useTheme } from '../../context/ThemeContext';
import { SunIcon, MoonIcon } from './Icons';

interface ThemeToggleProps {
  className?: string;
}

export const ThemeToggle: React.FC<ThemeToggleProps> = ({ className = '' }) => {
  const { toggleTheme, isDark } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={`Switch to ${isDark ? 'Light' : 'Dark'} Mode`}
      title={`Current: ${isDark ? 'Dark Mode' : 'Light Mode'} (Click to switch)`}
      className={`theme-toggle-btn ${className}`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: '38px',
        height: '38px',
        borderRadius: 'var(--radius-md)',
        background: 'var(--surface-glass-base)',
        border: 'var(--border-card)',
        color: isDark ? 'var(--accent-amber)' : 'var(--accent-primary)',
        cursor: 'pointer',
        transition: 'all var(--transition-fast)',
        position: 'relative',
        boxShadow: 'var(--shadow-sm)',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = 'var(--surface-glass-hover)';
        e.currentTarget.style.borderColor = 'var(--border-highlight)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = 'var(--surface-glass-base)';
        e.currentTarget.style.borderColor = 'var(--border-card)';
      }}
    >
      {isDark ? (
        <SunIcon size={19} className="theme-icon sun-icon" />
      ) : (
        <MoonIcon size={19} className="theme-icon moon-icon" />
      )}
    </button>
  );
};
