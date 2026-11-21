import React, { useEffect, useState } from 'react';
import { useTheme } from '../../context/ThemeContext';
import { useAuth } from '../../context/AuthContext';
import { LiquidToggle, NimbusToggleIcons } from '../ui/liquid-glass-toggle';
import { SearchIcon, BellIcon, ShieldCheckIcon } from '../common/Icons';

export const TopBar: React.FC = () => {
  const { isDark, toggleTheme } = useTheme();
  const {
    activeSession,
    currentUser,
    loginAs,
  } = useAuth();

  const [backendHealth, setBackendHealth] = useState<'healthy' | 'checking' | 'offline'>('checking');

  useEffect(() => {
    // Check backend readiness via active endpoint
    const checkHealth = async () => {
      try {
        const res = await fetch('/api/v1/health/live');
        if (res.ok) {
          setBackendHealth('healthy');
        } else {
          setBackendHealth('offline');
        }
      } catch {
        setBackendHealth('offline');
      }
    };

    checkHealth();
    const interval = setInterval(checkHealth, 30000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header
      className="app-topbar"
      style={{
        height: '64px',
        position: 'sticky',
        top: 0,
        zIndex: 50,
        background: 'var(--bg-topbar)',
        backdropFilter: 'var(--blur-lg)',
        WebkitBackdropFilter: 'var(--blur-lg)',
        borderBottom: 'var(--border-subtle)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 28px',
        transition: 'all var(--transition-normal)',
      }}
    >
      {/* Brand Identity & Search Bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '24px', flex: 1, maxWidth: '640px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', textDecoration: 'none', flexShrink: 0 }}>
          <div
            style={{
              width: '36px',
              height: '36px',
              borderRadius: 'var(--radius-md)',
              background: 'linear-gradient(135deg, var(--accent-primary), var(--accent-cyan))',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#ffffff',
              boxShadow: 'var(--shadow-glow-blue)',
            }}
          >
            <ShieldCheckIcon size={20} />
          </div>
          <div>
            <div style={{ fontSize: '0.9375rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
              NexusOps
            </div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
              Command Center
            </div>
          </div>
        </div>

        {/* Search Bar */}
        <div style={{ position: 'relative', flex: 1, maxWidth: '340px' }}>
          <SearchIcon
            size={16}
            style={{
              position: 'absolute',
              left: '12px',
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--text-muted)',
            }}
          />
          <input
            type="text"
            placeholder="Search tickets, customers, policies..."
            style={{
              width: '100%',
              padding: '8px 48px 8px 36px',
              borderRadius: 'var(--radius-full)',
              background: 'var(--surface-glass-input)',
              border: 'var(--border-card)',
              color: 'var(--text-primary)',
              fontSize: '0.8125rem',
              outline: 'none',
              transition: 'all var(--transition-fast)',
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = 'var(--border-highlight)';
              e.currentTarget.style.boxShadow = 'var(--shadow-glow-blue)';
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = 'var(--border-card)';
              e.currentTarget.style.boxShadow = 'none';
            }}
          />
          <span
            style={{
              position: 'absolute',
              right: '10px',
              top: '50%',
              transform: 'translateY(-50%)',
              fontSize: '0.6875rem',
              fontFamily: 'var(--font-mono)',
              padding: '2px 6px',
              borderRadius: '4px',
              background: 'var(--surface-glass-elevated)',
              border: 'var(--border-subtle)',
              color: 'var(--text-muted)',
            }}
          >
            Ctrl+K
          </span>
        </div>
      </div>

      {/* Right Controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
        {/* Backend Status Indicator */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            padding: '4px 12px',
            borderRadius: 'var(--radius-full)',
            background: 'var(--surface-glass-base)',
            border: 'var(--border-card)',
            fontSize: '0.75rem',
            fontWeight: 500,
            color: 'var(--text-secondary)',
          }}
        >
          <span
            style={{
              width: '8px',
              height: '8px',
              borderRadius: '50%',
              backgroundColor:
                backendHealth === 'healthy'
                  ? 'var(--accent-emerald)'
                  : backendHealth === 'checking'
                  ? 'var(--accent-amber)'
                  : 'var(--accent-rose)',
              boxShadow:
                backendHealth === 'healthy'
                  ? '0 0 8px var(--accent-emerald)'
                  : 'none',
            }}
          />
          <span>API: {backendHealth === 'healthy' ? 'Online' : backendHealth === 'checking' ? 'Connecting' : 'Offline'}</span>
        </div>

        {/* Security Role Indicator & Quick Sign-in */}
        {(!activeSession || (!currentUser?.roles?.length && !activeSession.roles?.length && activeSession.auth_type !== 'SESSION_TOKEN')) ? (
          <button
            type="button"
            onClick={() => loginAs('agent')}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              padding: '5px 12px',
              borderRadius: 'var(--radius-md)',
              background: 'rgba(239, 68, 68, 0.15)',
              border: '1px solid rgba(239, 68, 68, 0.4)',
              color: 'var(--accent-rose)',
              fontSize: '0.75rem',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all var(--transition-fast)',
            }}
            title="Click to sign in as Staff Agent"
          >
            <ShieldCheckIcon size={14} />
            <span>Sign In (Agent)</span>
          </button>
        ) : (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              padding: '5px 12px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--accent-primary-light)',
              border: '1px solid rgba(59, 130, 246, 0.25)',
              color: 'var(--accent-primary)',
              fontSize: '0.75rem',
              fontWeight: 600,
            }}
            title={`Signed in as ${activeSession?.display_name || 'Agent'}`}
          >
            <ShieldCheckIcon size={14} />
            <span>
              Role:{' '}
              {currentUser?.roles?.[0] ||
                activeSession?.roles?.[0] ||
                (activeSession?.auth_type === 'SESSION_TOKEN' ? 'CUSTOMER' : 'AGENT')}
            </span>
          </div>
        )}

        {/* Notifications Icon Button */}
        <button
          type="button"
          aria-label="View notifications"
          style={{
            width: '38px',
            height: '38px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--surface-glass-base)',
            border: 'var(--border-card)',
            color: 'var(--text-secondary)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: 'pointer',
            position: 'relative',
            transition: 'all var(--transition-fast)',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = 'var(--surface-glass-hover)';
            e.currentTarget.style.color = 'var(--text-primary)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'var(--surface-glass-base)';
            e.currentTarget.style.color = 'var(--text-secondary)';
          }}
        >
          <BellIcon size={18} />
          <span
            style={{
              position: 'absolute',
              top: '8px',
              right: '8px',
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              backgroundColor: 'var(--accent-rose)',
            }}
          />
        </button>

        {/* Liquid Glass Dark / Light Mode Switch */}
        <LiquidToggle
          size={58}
          tone={isDark ? 'dark' : 'light'}
          checked={isDark}
          onCheckedChange={() => toggleTheme()}
          icons={{ off: NimbusToggleIcons.sun, on: NimbusToggleIcons.moon }}
          label={isDark ? 'Dark mode active. Click to switch to Light mode' : 'Light mode active. Click to switch to Dark mode'}
        />
      </div>
    </header>
  );
};

