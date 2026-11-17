import React from 'react';
import { useTheme } from '../../context/ThemeContext';
import { LiquidToggle, NimbusToggleIcons } from '../ui/liquid-glass-toggle';

interface ThemeToggleProps {
  className?: string;
  size?: number;
}

export const ThemeToggle: React.FC<ThemeToggleProps> = ({ className = '', size = 58 }) => {
  const { toggleTheme, isDark } = useTheme();

  return (
    <LiquidToggle
      size={size}
      tone={isDark ? 'dark' : 'light'}
      checked={isDark}
      onCheckedChange={() => toggleTheme()}
      icons={{ off: NimbusToggleIcons.sun, on: NimbusToggleIcons.moon }}
      label={isDark ? 'Dark mode active. Click to switch to Light mode' : 'Light mode active. Click to switch to Dark mode'}
      className={className}
    />
  );
};
