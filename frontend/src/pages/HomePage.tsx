import React from 'react';
import { Link } from 'react-router-dom';
import {
  SparklesIcon,
  TicketIcon,
  ClockIcon,
  ShieldCheckIcon,
  BotIcon,
  CheckCircleIcon,
  DashboardIcon,
  LockIcon,
  LayersIcon,
} from '../components/common/Icons';
import { ParticleTitle } from '../components/ui/particle-title';

export const HomePage: React.FC = () => {
  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#070a12',
        color: '#f8fafc',
        fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* =========================================================================
          PUBLIC NAVIGATION HEADER
          ========================================================================= */}
      <header
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 50,
          background: 'rgba(7, 10, 18, 0.82)',
          backdropFilter: 'blur(16px)',
          WebkitBackdropFilter: 'blur(16px)',
          borderBottom: '1px solid rgba(255, 255, 255, 0.07)',
        }}
      >
        <div
          style={{
            maxWidth: '1240px',
            margin: '0 auto',
            padding: '16px 24px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '16px',
          }}
        >
          {/* Logo & Brand Identity */}
          <Link
            to="/"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
              textDecoration: 'none',
              color: '#ffffff',
              flexShrink: 0,
            }}
          >
            <div
              style={{
                width: '32px',
                height: '32px',
                borderRadius: '8px',
                background: 'linear-gradient(135deg, #2563eb, #06b6d4)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                boxShadow: '0 2px 8px rgba(37, 99, 235, 0.35)',
                flexShrink: 0,
              }}
            >
              <BotIcon size={18} style={{ color: '#ffffff' }} />
            </div>
            <span
              style={{
                fontSize: '0.9375rem',
                fontWeight: 600,
                letterSpacing: '-0.01em',
                color: '#f8fafc',
                whiteSpace: 'nowrap',
              }}
            >
              AI Customer Support Platform
            </span>
          </Link>

          {/* Navigation Anchors */}
          <nav
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '28px',
            }}
            className="homepage-nav-links"
          >
            <a
              href="#features"
              style={{
                color: '#94a3b8',
                fontSize: '0.875rem',
                fontWeight: 500,
                textDecoration: 'none',
                transition: 'color 0.15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
              onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
            >
              Features
            </a>
            <a
              href="#how-it-works"
              style={{
                color: '#94a3b8',
                fontSize: '0.875rem',
                fontWeight: 500,
                textDecoration: 'none',
                transition: 'color 0.15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
              onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
            >
              How It Works
            </a>
            <a
              href="#security"
              style={{
                color: '#94a3b8',
                fontSize: '0.875rem',
                fontWeight: 500,
                textDecoration: 'none',
                transition: 'color 0.15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
              onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
            >
              Security
            </a>
          </nav>

          {/* Direct Workspace Actions */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <Link
              to="/app"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                padding: '8px 16px',
                borderRadius: '8px',
                fontSize: '0.8125rem',
                fontWeight: 600,
                color: '#f8fafc',
                background: 'rgba(255, 255, 255, 0.05)',
                border: '1px solid rgba(255, 255, 255, 0.12)',
                textDecoration: 'none',
                transition: 'all 0.15s ease',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.09)';
                e.currentTarget.style.borderColor = 'rgba(255, 255, 255, 0.2)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
                e.currentTarget.style.borderColor = 'rgba(255, 255, 255, 0.12)';
              }}
            >
              <DashboardIcon size={14} />
              <span>Launch App</span>
            </Link>
          </div>
        </div>
      </header>

      {/* =========================================================================
          HERO SECTION — GRAND PARTICLE OPENING STAGE
          - Full-Width Centered Particle Title Stage
          - Glowing Ambient Radial Fields (Smoke, Vortex, Sand, Burst, Sweep, Absorb)
          - Centered Subtitle, CTAs & Premium Office Monitor Preview
          ========================================================================= */}
      <section
        style={{
          padding: '64px 24px 88px 24px',
          maxWidth: '1240px',
          margin: '0 auto',
          width: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          textAlign: 'center',
        }}
      >
        {/* Small Eyebrow Label */}
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '8px',
            padding: '6px 16px',
            borderRadius: '9999px',
            background: 'rgba(56, 189, 248, 0.08)',
            border: '1px solid rgba(56, 189, 248, 0.25)',
            width: 'fit-content',
            marginBottom: '12px',
          }}
        >
          <span
            style={{
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              background: '#38bdf8',
              boxShadow: '0 0 8px #38bdf8',
            }}
          />
          <span
            style={{
              fontSize: '0.75rem',
              fontWeight: 700,
              letterSpacing: '0.12em',
              textTransform: 'uppercase',
              color: '#38bdf8',
            }}
          >
            AI Customer Support Platform
          </span>
        </div>

        {/* Grand Full-Width Particle Title Opening Stage */}
        <div style={{ width: '100%', maxWidth: '1040px', margin: '0 auto' }}>
          <ParticleTitle
            text="Customer Support. Intelligently Resolved."
            as="h1"
            height={340}
          />
        </div>

        {/* Short Description */}
        <p
          style={{
            fontSize: '1.125rem',
            lineHeight: 1.65,
            color: '#94a3b8',
            maxWidth: '680px',
            margin: '8px auto 28px auto',
          }}
        >
          Enterprise-grade customer support platform combining multi-turn RAG resolution,
          automated ticket management, and deterministic SLA protection with strict tenant isolation.
        </p>

        {/* Action Buttons */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '14px',
            flexWrap: 'wrap',
            marginBottom: '54px',
          }}
        >
          <Link
            to="/conversations"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '8px',
              padding: '13px 28px',
              borderRadius: '12px',
              background: '#2563eb',
              color: '#ffffff',
              fontSize: '0.9375rem',
              fontWeight: 600,
              textDecoration: 'none',
              boxShadow: '0 4px 18px rgba(37, 99, 235, 0.4)',
              transition: 'background 0.15s ease, transform 0.1s ease',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = '#1d4ed8';
              e.currentTarget.style.transform = 'translateY(-1px)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = '#2563eb';
              e.currentTarget.style.transform = 'translateY(0)';
            }}
          >
            <SparklesIcon size={16} />
            <span>Launch AI Assistant</span>
          </Link>

          <Link
            to="/app"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '8px',
              padding: '13px 28px',
              borderRadius: '12px',
              background: 'rgba(255, 255, 255, 0.05)',
              color: '#f8fafc',
              fontSize: '0.9375rem',
              fontWeight: 600,
              border: '1px solid rgba(255, 255, 255, 0.14)',
              textDecoration: 'none',
              transition: 'all 0.15s ease',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = 'rgba(255, 255, 255, 0.08)';
              e.currentTarget.style.borderColor = 'rgba(255, 255, 255, 0.22)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
              e.currentTarget.style.borderColor = 'rgba(255, 255, 255, 0.14)';
            }}
          >
            <DashboardIcon size={16} />
            <span>Open Command Center</span>
          </Link>
        </div>

        {/* Centerpiece Visual: Office Monitor Photograph */}
        <div
          style={{
            position: 'relative',
            width: '100%',
            maxWidth: '1080px',
            borderRadius: '20px',
            border: '1px solid rgba(255, 255, 255, 0.12)',
            boxShadow: '0 24px 64px -12px rgba(0, 0, 0, 0.75)',
            overflow: 'hidden',
            background: '#0d131f',
          }}
        >
          <picture>
            <source srcSet="/hero-monitor.webp" type="image/webp" />
            <img
              src="/hero-monitor.png"
              alt="AI Customer Support Platform Office Monitor"
              loading="eager"
              style={{
                width: '100%',
                height: 'auto',
                display: 'block',
                objectFit: 'cover',
              }}
            />
          </picture>
        </div>
      </section>

      {/* =========================================================================
          FEATURES SECTION
          - Three Clean Feature Blocks
          1. AI-Powered Resolution
          2. Intelligent Ticket Management
          3. SLA Monitoring
          ========================================================================= */}
      <section
        id="features"
        style={{
          padding: '80px 24px',
          maxWidth: '1240px',
          margin: '0 auto',
          width: '100%',
          borderTop: '1px solid rgba(255, 255, 255, 0.06)',
        }}
      >
        <div style={{ maxWidth: '640px', marginBottom: '48px' }}>
          <p
            style={{
              fontSize: '0.6875rem',
              fontWeight: 700,
              letterSpacing: '0.15em',
              textTransform: 'uppercase',
              color: '#38bdf8',
              margin: '0 0 10px 0',
            }}
          >
            Capabilities
          </p>
          <h2
            style={{
              fontSize: 'clamp(1.75rem, 3vw, 2.25rem)',
              fontWeight: 700,
              letterSpacing: '-0.025em',
              color: '#ffffff',
              margin: '0 0 12px 0',
            }}
          >
            Engineered for enterprise resolution
          </h2>
          <p
            style={{
              fontSize: '1rem',
              lineHeight: 1.6,
              color: '#94a3b8',
              margin: 0,
            }}
          >
            Three core architectural engines working together to resolve inquiries accurately,
            organize operational workflows, and safeguard service level commitments.
          </p>
        </div>

        {/* 3-Column Feature Grid */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
            gap: '24px',
          }}
        >
          {/* Feature 1 */}
          <div
            style={{
              padding: '32px 28px',
              borderRadius: '14px',
              background: 'rgba(15, 23, 42, 0.45)',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
          >
            <div
              style={{
                width: '40px',
                height: '40px',
                borderRadius: '10px',
                background: 'rgba(56, 189, 248, 0.1)',
                border: '1px solid rgba(56, 189, 248, 0.25)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#38bdf8',
              }}
            >
              <SparklesIcon size={20} />
            </div>
            <h3
              style={{
                fontSize: '1.125rem',
                fontWeight: 600,
                color: '#ffffff',
                margin: 0,
              }}
            >
              AI-Powered Resolution
            </h3>
            <p
              style={{
                fontSize: '0.9375rem',
                lineHeight: 1.6,
                color: '#94a3b8',
                margin: 0,
              }}
            >
              Multi-turn RAG with knowledge-grounded responses. Eliminates hallucinations
              by strictly constraining generative models to verified organizational documentation.
            </p>
          </div>

          {/* Feature 2 */}
          <div
            style={{
              padding: '32px 28px',
              borderRadius: '14px',
              background: 'rgba(15, 23, 42, 0.45)',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
          >
            <div
              style={{
                width: '40px',
                height: '40px',
                borderRadius: '10px',
                background: 'rgba(59, 130, 246, 0.1)',
                border: '1px solid rgba(59, 130, 246, 0.25)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#60a5fa',
              }}
            >
              <TicketIcon size={20} />
            </div>
            <h3
              style={{
                fontSize: '1.125rem',
                fontWeight: 600,
                color: '#ffffff',
                margin: 0,
              }}
            >
              Intelligent Ticket Management
            </h3>
            <p
              style={{
                fontSize: '0.9375rem',
                lineHeight: 1.6,
                color: '#94a3b8',
                margin: 0,
              }}
            >
              Prioritization, assignment and escalation workflows. Routes complex cases
              to appropriate engineering teams with complete interaction context.
            </p>
          </div>

          {/* Feature 3 */}
          <div
            style={{
              padding: '32px 28px',
              borderRadius: '14px',
              background: 'rgba(15, 23, 42, 0.45)',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
          >
            <div
              style={{
                width: '40px',
                height: '40px',
                borderRadius: '10px',
                background: 'rgba(16, 185, 129, 0.1)',
                border: '1px solid rgba(16, 185, 129, 0.25)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#34d399',
              }}
            >
              <ClockIcon size={20} />
            </div>
            <h3
              style={{
                fontSize: '1.125rem',
                fontWeight: 600,
                color: '#ffffff',
                margin: 0,
              }}
            >
              SLA Monitoring
            </h3>
            <p
              style={{
                fontSize: '0.9375rem',
                lineHeight: 1.6,
                color: '#94a3b8',
                margin: 0,
              }}
            >
              Track deadlines and identify cases requiring intervention. Automated watchdogs
              monitor resolution windows and trigger proactive on-call alerts before breach.
            </p>
          </div>
        </div>
      </section>

      {/* =========================================================================
          HOW IT WORKS SECTION
          - Three Steps: Ingest & Classify -> Knowledge Grounding -> Resolution & Escalation
          ========================================================================= */}
      <section
        id="how-it-works"
        style={{
          padding: '80px 24px',
          maxWidth: '1240px',
          margin: '0 auto',
          width: '100%',
          borderTop: '1px solid rgba(255, 255, 255, 0.06)',
        }}
      >
        <div style={{ maxWidth: '640px', marginBottom: '48px' }}>
          <p
            style={{
              fontSize: '0.6875rem',
              fontWeight: 700,
              letterSpacing: '0.15em',
              textTransform: 'uppercase',
              color: '#38bdf8',
              margin: '0 0 10px 0',
            }}
          >
            Architecture
          </p>
          <h2
            style={{
              fontSize: 'clamp(1.75rem, 3vw, 2.25rem)',
              fontWeight: 700,
              letterSpacing: '-0.025em',
              color: '#ffffff',
              margin: '0 0 12px 0',
            }}
          >
            How it works
          </h2>
          <p
            style={{
              fontSize: '1rem',
              lineHeight: 1.6,
              color: '#94a3b8',
              margin: 0,
            }}
          >
            A continuous, audited pipeline from customer inquiry to verified resolution.
          </p>
        </div>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
            gap: '24px',
          }}
        >
          {/* Step 1 */}
          <div
            style={{
              padding: '32px 28px',
              borderRadius: '14px',
              background: 'rgba(15, 23, 42, 0.3)',
              border: '1px solid rgba(255, 255, 255, 0.06)',
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
            }}
          >
            <span
              style={{
                fontSize: '0.8125rem',
                fontWeight: 700,
                color: '#64748b',
                letterSpacing: '0.1em',
              }}
            >
              01
            </span>
            <h3 style={{ fontSize: '1.0625rem', fontWeight: 600, color: '#ffffff', margin: 0 }}>
              Ingest & Normalize
            </h3>
            <p style={{ fontSize: '0.9375rem', lineHeight: 1.6, color: '#94a3b8', margin: 0 }}>
              Customer messages across channels are ingested, normalized, screened for adversarial inputs,
              and stripped of sensitive payment and personal identifiers.
            </p>
          </div>

          {/* Step 2 */}
          <div
            style={{
              padding: '32px 28px',
              borderRadius: '14px',
              background: 'rgba(15, 23, 42, 0.3)',
              border: '1px solid rgba(255, 255, 255, 0.06)',
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
            }}
          >
            <span
              style={{
                fontSize: '0.8125rem',
                fontWeight: 700,
                color: '#64748b',
                letterSpacing: '0.1em',
              }}
            >
              02
            </span>
            <h3 style={{ fontSize: '1.0625rem', fontWeight: 600, color: '#ffffff', margin: 0 }}>
              Knowledge-Grounded Synthesis
            </h3>
            <p style={{ fontSize: '0.9375rem', lineHeight: 1.6, color: '#94a3b8', margin: 0 }}>
              Multi-tenant vector retrieval finds authenticated knowledge chunks and generates
              cited, policy-compliant responses within strict tenant isolation boundaries.
            </p>
          </div>

          {/* Step 3 */}
          <div
            style={{
              padding: '32px 28px',
              borderRadius: '14px',
              background: 'rgba(15, 23, 42, 0.3)',
              border: '1px solid rgba(255, 255, 255, 0.06)',
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
            }}
          >
            <span
              style={{
                fontSize: '0.8125rem',
                fontWeight: 700,
                color: '#64748b',
                letterSpacing: '0.1em',
              }}
            >
              03
            </span>
            <h3 style={{ fontSize: '1.0625rem', fontWeight: 600, color: '#ffffff', margin: 0 }}>
              Resolution & Safe Escalation
            </h3>
            <p style={{ fontSize: '0.9375rem', lineHeight: 1.6, color: '#94a3b8', margin: 0 }}>
              Cases are resolved autonomously or escalated to on-call agents with complete audit trails,
              evidence dossiers, and active SLA countdown safeguards.
            </p>
          </div>
        </div>
      </section>

      {/* =========================================================================
          SECURITY AND RELIABILITY SECTION
          - Enterprise Tenant Isolation, PII Protection, Encryption, Auditability
          ========================================================================= */}
      <section
        id="security"
        style={{
          padding: '80px 24px',
          maxWidth: '1240px',
          margin: '0 auto',
          width: '100%',
          borderTop: '1px solid rgba(255, 255, 255, 0.06)',
        }}
      >
        <div style={{ maxWidth: '640px', marginBottom: '48px' }}>
          <p
            style={{
              fontSize: '0.6875rem',
              fontWeight: 700,
              letterSpacing: '0.15em',
              textTransform: 'uppercase',
              color: '#38bdf8',
              margin: '0 0 10px 0',
            }}
          >
            Trust & Security
          </p>
          <h2
            style={{
              fontSize: 'clamp(1.75rem, 3vw, 2.25rem)',
              fontWeight: 700,
              letterSpacing: '-0.025em',
              color: '#ffffff',
              margin: '0 0 12px 0',
            }}
          >
            Security and reliability by design
          </h2>
          <p
            style={{
              fontSize: '1rem',
              lineHeight: 1.6,
              color: '#94a3b8',
              margin: 0,
            }}
          >
            Built for enterprise environments requiring strict isolation, deterministic safeguards, and compliance.
          </p>
        </div>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
            gap: '24px',
          }}
        >
          <div
            style={{
              padding: '28px 24px',
              borderRadius: '12px',
              background: 'rgba(15, 23, 42, 0.35)',
              border: '1px solid rgba(255, 255, 255, 0.07)',
            }}
          >
            <ShieldCheckIcon size={20} style={{ color: '#38bdf8', marginBottom: '14px' }} />
            <h4 style={{ fontSize: '1rem', fontWeight: 600, color: '#ffffff', margin: '0 0 8px 0' }}>
              Strict Tenant Isolation
            </h4>
            <p style={{ fontSize: '0.875rem', lineHeight: 1.55, color: '#94a3b8', margin: 0 }}>
              Enforced tenant boundaries across database records, vector embeddings, and cache keys.
              Cross-tenant access is structurally impossible.
            </p>
          </div>

          <div
            style={{
              padding: '28px 24px',
              borderRadius: '12px',
              background: 'rgba(15, 23, 42, 0.35)',
              border: '1px solid rgba(255, 255, 255, 0.07)',
            }}
          >
            <CheckCircleIcon size={20} style={{ color: '#34d399', marginBottom: '14px' }} />
            <h4 style={{ fontSize: '1rem', fontWeight: 600, color: '#ffffff', margin: '0 0 8px 0' }}>
              Zero-Logging PII Masking
            </h4>
            <p style={{ fontSize: '0.875rem', lineHeight: 1.55, color: '#94a3b8', margin: 0 }}>
              Automatic real-time redaction of credit cards (Luhn mod-10), SSNs, and phone numbers
              before any persistence or model transmission occurs.
            </p>
          </div>

          <div
            style={{
              padding: '28px 24px',
              borderRadius: '12px',
              background: 'rgba(15, 23, 42, 0.35)',
              border: '1px solid rgba(255, 255, 255, 0.07)',
            }}
          >
            <LockIcon size={20} style={{ color: '#60a5fa', marginBottom: '14px' }} />
            <h4 style={{ fontSize: '1rem', fontWeight: 600, color: '#ffffff', margin: '0 0 8px 0' }}>
              Application Encryption
            </h4>
            <p style={{ fontSize: '0.875rem', lineHeight: 1.55, color: '#94a3b8', margin: 0 }}>
              Sensitive customer payloads are protected with AES-256-GCM encryption with cryptographic
              nonces and associated data bound to record keys.
            </p>
          </div>

          <div
            style={{
              padding: '28px 24px',
              borderRadius: '12px',
              background: 'rgba(15, 23, 42, 0.35)',
              border: '1px solid rgba(255, 255, 255, 0.07)',
            }}
          >
            <LayersIcon size={20} style={{ color: '#a78bfa', marginBottom: '14px' }} />
            <h4 style={{ fontSize: '1rem', fontWeight: 600, color: '#ffffff', margin: '0 0 8px 0' }}>
              Cryptographic Auditability
            </h4>
            <p style={{ fontSize: '0.875rem', lineHeight: 1.55, color: '#94a3b8', margin: 0 }}>
              Every escalation, evidence document, and automated playbook execution leaves a verifiable,
              tamper-evident audit trail.
            </p>
          </div>
        </div>
      </section>

      {/* =========================================================================
          FINAL CALL TO ACTION
          ========================================================================= */}
      <section
        style={{
          padding: '80px 24px',
          maxWidth: '1240px',
          margin: '0 auto',
          width: '100%',
        }}
      >
        <div
          style={{
            borderRadius: '18px',
            background: 'linear-gradient(180deg, rgba(30, 41, 59, 0.4) 0%, rgba(15, 23, 42, 0.6) 100%)',
            border: '1px solid rgba(255, 255, 255, 0.09)',
            padding: '64px 32px',
            textAlign: 'center',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '24px',
          }}
        >
          <div style={{ maxWidth: '580px' }}>
            <h2
              style={{
                fontSize: 'clamp(2rem, 3.5vw, 2.75rem)',
                fontWeight: 700,
                letterSpacing: '-0.03em',
                color: '#ffffff',
                margin: '0 0 12px 0',
              }}
            >
              Ready to elevate your customer support operations?
            </h2>
            <p
              style={{
                fontSize: '1rem',
                lineHeight: 1.6,
                color: '#94a3b8',
                margin: 0,
              }}
            >
              Experience knowledge-grounded AI resolution with deterministic SLA safeguards and full enterprise compliance.
            </p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap' }}>
            <Link
              to="/conversations"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                padding: '12px 24px',
                borderRadius: '10px',
                background: '#2563eb',
                color: '#ffffff',
                fontSize: '0.9375rem',
                fontWeight: 600,
                textDecoration: 'none',
                boxShadow: '0 4px 14px rgba(37, 99, 235, 0.3)',
                transition: 'background 0.15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = '#1d4ed8')}
              onMouseLeave={(e) => (e.currentTarget.style.background = '#2563eb')}
            >
              <SparklesIcon size={16} />
              <span>Launch AI Assistant</span>
            </Link>

            <Link
              to="/app"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                padding: '12px 24px',
                borderRadius: '10px',
                background: 'rgba(255, 255, 255, 0.05)',
                color: '#f8fafc',
                fontSize: '0.9375rem',
                fontWeight: 600,
                border: '1px solid rgba(255, 255, 255, 0.12)',
                textDecoration: 'none',
                transition: 'background 0.15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'rgba(255, 255, 255, 0.08)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)')}
            >
              <span>Explore Platform</span>
            </Link>
          </div>
        </div>
      </section>

      {/* =========================================================================
          MINIMAL FOOTER
          ========================================================================= */}
      <footer
        style={{
          borderTop: '1px solid rgba(255, 255, 255, 0.07)',
          padding: '48px 24px',
          marginTop: 'auto',
          background: 'rgba(7, 10, 18, 0.95)',
        }}
      >
        <div
          style={{
            maxWidth: '1240px',
            margin: '0 auto',
            display: 'flex',
            flexDirection: 'column',
            gap: '32px',
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: '24px',
            }}
          >
            {/* Left: Brand Identity */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <div
                style={{
                  width: '24px',
                  height: '24px',
                  borderRadius: '6px',
                  background: 'linear-gradient(135deg, #2563eb, #06b6d4)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <BotIcon size={14} style={{ color: '#ffffff' }} />
              </div>
              <span style={{ fontSize: '0.875rem', fontWeight: 600, color: '#f8fafc' }}>
                AI Customer Support Platform
              </span>
            </div>

            {/* Quick Links */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '24px',
                flexWrap: 'wrap',
              }}
            >
              <Link
                to="/app"
                style={{ color: '#94a3b8', fontSize: '0.8125rem', textDecoration: 'none' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
                onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
              >
                Overview
              </Link>
              <Link
                to="/conversations"
                style={{ color: '#94a3b8', fontSize: '0.8125rem', textDecoration: 'none' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
                onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
              >
                Conversations
              </Link>
              <Link
                to="/tickets"
                style={{ color: '#94a3b8', fontSize: '0.8125rem', textDecoration: 'none' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
                onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
              >
                Tickets
              </Link>
              <Link
                to="/app/escalations"
                style={{ color: '#94a3b8', fontSize: '0.8125rem', textDecoration: 'none' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
                onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
              >
                Escalations
              </Link>
              <Link
                to="/knowledge"
                style={{ color: '#94a3b8', fontSize: '0.8125rem', textDecoration: 'none' }}
                onMouseEnter={(e) => (e.currentTarget.style.color = '#ffffff')}
                onMouseLeave={(e) => (e.currentTarget.style.color = '#94a3b8')}
              >
                Knowledge Base
              </Link>
            </div>

            {/* System Status Indicator */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                fontSize: '0.8125rem',
                color: '#94a3b8',
              }}
            >
              <span
                style={{
                  width: '8px',
                  height: '8px',
                  borderRadius: '50%',
                  background: '#10b981',
                  boxShadow: '0 0 8px rgba(16, 185, 129, 0.6)',
                }}
              />
              <span>All Systems Operational</span>
            </div>
          </div>

          <div
            style={{
              borderTop: '1px solid rgba(255, 255, 255, 0.05)',
              paddingTop: '20px',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: '12px',
              fontSize: '0.75rem',
              color: '#64748b',
            }}
          >
            <span>© 2026 AI Customer Support Platform. Enterprise-grade AI architecture.</span>
            <span>Zero-trust security · Multi-tenant RAG · Deterministic SLA watchdogs</span>
          </div>
        </div>
      </footer>
    </div>
  );
};
