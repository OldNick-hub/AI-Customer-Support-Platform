import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  DashboardIcon,
  MessageSquareIcon,
  TicketIcon,
  AlertTriangleIcon,
  FileTextIcon,
  DatabaseIcon,
  BarChart3Icon,
  SettingsIcon,
  ShieldCheckIcon,
} from '../common/Icons';

export const Sidebar: React.FC = () => {
  const navItems = [
    { to: '/', label: 'Overview', icon: <DashboardIcon size={18} /> },
    { to: '/conversations', label: 'Conversations', icon: <MessageSquareIcon size={18} /> },
    { to: '/tickets', label: 'Ticket Queue', icon: <TicketIcon size={18} /> },
    { to: '/escalations', label: 'Escalation Center', icon: <AlertTriangleIcon size={18} /> },
    { to: '/evidence', label: 'Evidence Vault', icon: <FileTextIcon size={18} /> },
    { to: '/knowledge', label: 'Knowledge Base', icon: <DatabaseIcon size={18} /> },
    { to: '/analytics', label: 'Analytics', icon: <BarChart3Icon size={18} /> },
    { to: '/settings', label: 'Settings', icon: <SettingsIcon size={18} /> },
  ];

  return (
    <aside
      className="app-sidebar"
      style={{
        width: '260px',
        height: '100vh',
        position: 'fixed',
        left: 0,
        top: 0,
        background: 'var(--bg-sidebar)',
        backdropFilter: 'var(--blur-lg)',
        WebkitBackdropFilter: 'var(--blur-lg)',
        borderRight: 'var(--border-subtle)',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 50,
        transition: 'all var(--transition-normal)',
      }}
    >
      {/* Brand Header */}
      <div
        style={{
          padding: '24px 20px',
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
          borderBottom: 'var(--border-subtle)',
        }}
      >
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
            ApexSupport AI
          </div>
          <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: 'var(--accent-emerald)', display: 'inline-block' }} />
            Enterprise v0.1.0
          </div>
        </div>
      </div>

      {/* Navigation List */}
      <nav style={{ padding: '16px 12px', flex: 1, overflowY: 'auto' }}>
        <div style={{ fontSize: '0.6875rem', fontWeight: 600, textTransform: 'uppercase', color: 'var(--text-muted)', padding: '0 12px 8px', letterSpacing: '0.08em' }}>
          Workspace
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              style={({ isActive }) => ({
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                padding: '10px 14px',
                borderRadius: 'var(--radius-md)',
                color: isActive ? 'var(--accent-primary)' : 'var(--text-secondary)',
                backgroundColor: isActive ? 'var(--accent-primary-light)' : 'transparent',
                fontWeight: isActive ? 600 : 500,
                fontSize: '0.875rem',
                textDecoration: 'none',
                transition: 'all var(--transition-fast)',
                border: isActive ? '1px solid rgba(59, 130, 246, 0.25)' : '1px solid transparent',
              })}
            >
              <span style={{ display: 'flex', alignItems: 'center' }}>{item.icon}</span>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </div>
      </nav>

      {/* Agent & On-Call Footer */}
      <div
        style={{
          padding: '16px',
          borderTop: 'var(--border-subtle)',
          background: 'var(--surface-glass-base)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <div
            style={{
              width: '34px',
              height: '34px',
              borderRadius: '50%',
              backgroundColor: 'var(--surface-glass-elevated)',
              border: 'var(--border-card)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--text-primary)',
              fontWeight: 700,
              fontSize: '0.8125rem',
            }}
          >
            SA
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="truncate" style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--text-primary)' }}>
              Support Agent (Dev)
            </div>
            <div style={{ fontSize: '0.6875rem', color: 'var(--text-muted)' }}>
              Hardware Tier • On-Call
            </div>
          </div>
          <div
            style={{
              width: '8px',
              height: '8px',
              borderRadius: '50%',
              backgroundColor: 'var(--accent-emerald)',
              boxShadow: '0 0 8px var(--accent-emerald)',
            }}
            title="Agent Online & On-Call"
          />
        </div>
      </div>
    </aside>
  );
};

