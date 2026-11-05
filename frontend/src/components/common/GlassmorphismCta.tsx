import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { SparklesIcon, BotIcon } from './Icons';

export interface GlassmorphismCtaProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  label?: string;
  avatarSrc?: string;
  avatarAlt?: string;
  to?: string;
  icon?: React.ReactNode;
  speed?: string;
  spread?: string;
  shimmerColor?: string;
  size?: 'sm' | 'md' | 'lg';
}

export const GlassmorphismCta: React.FC<GlassmorphismCtaProps> = ({
  label = 'Start AI Assistant',
  avatarSrc,
  avatarAlt = 'AI Copilot',
  to,
  icon = <SparklesIcon size={14} />,
  speed = '4s',
  spread = '90deg',
  shimmerColor = 'var(--cta-shimmer)',
  size = 'md',
  className = '',
  style,
  onClick,
  ...props
}) => {
  const navigate = useNavigate();
  const [imgError, setImgError] = useState(false);

  const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (onClick) {
      onClick(e);
    }
    if (to) {
      navigate(to);
    }
  };

  const avatarSize = size === 'sm' ? 24 : size === 'lg' ? 34 : 28;
  const iconContainerSize = size === 'sm' ? 22 : size === 'lg' ? 30 : 26;

  return (
    <button
      type="button"
      className={`glass-cta-btn ${className}`}
      onClick={handleClick}
      aria-label={label}
      style={{
        ...style,
      }}
      {...props}
    >
      {/* Layer 1: Conic Gradient Shimmer Rotating Perimeter */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          overflow: 'hidden',
          borderRadius: 'var(--radius-full)',
          pointerEvents: 'none',
        }}
      >
        <div
          className="glass-cta-shimmer-spin"
          style={{
            position: 'absolute',
            inset: '-150%',
            width: '400%',
            height: '400%',
            background: `conic-gradient(from calc(270deg - (${spread} * 0.5)), transparent 0, ${shimmerColor} ${spread}, transparent ${spread})`,
            animation: `glassCtaRotate ${speed} linear infinite`,
          }}
        />
      </div>

      {/* Layer 2: Rotating Ambient Border Beam */}
      <div
        className="glass-cta-beam"
        style={{
          position: 'absolute',
          width: '200%',
          height: '200%',
          background:
            'linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.15), rgba(255, 255, 255, 0.15), transparent)',
          animation: `borderBeamRotation ${speed} infinite linear`,
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          pointerEvents: 'none',
        }}
      />

      {/* Layer 3: Glassmorphism Inset Backdrop */}
      <div
        style={{
          position: 'absolute',
          inset: '1.5px',
          background: 'var(--surface-glass-card)',
          borderRadius: 'var(--radius-full)',
          backdropFilter: 'var(--blur-md)',
          WebkitBackdropFilter: 'var(--blur-md)',
          zIndex: 1,
        }}
      />

      {/* Layer 4: Interactive Content Payload */}
      <div
        style={{
          position: 'relative',
          zIndex: 2,
          display: 'flex',
          alignItems: 'center',
          gap: size === 'sm' ? '8px' : '10px',
          padding:
            size === 'sm'
              ? '3px 10px 3px 4px'
              : size === 'lg'
              ? '7px 18px 7px 8px'
              : '5px 14px 5px 6px',
          color: 'var(--text-primary)',
          fontSize: size === 'sm' ? '0.75rem' : size === 'lg' ? '0.9375rem' : '0.8125rem',
          fontWeight: 600,
          letterSpacing: '-0.01em',
        }}
      >
        {/* Left Avatar / AI Badge */}
        {avatarSrc && !imgError ? (
          <img
            src={avatarSrc}
            alt={avatarAlt}
            onError={() => setImgError(true)}
            style={{
              width: `${avatarSize}px`,
              height: `${avatarSize}px`,
              borderRadius: '50%',
              objectFit: 'cover',
              boxShadow: '0 0 0 2px var(--border-subtle)',
            }}
          />
        ) : (
          <div
            style={{
              width: `${avatarSize}px`,
              height: `${avatarSize}px`,
              borderRadius: '50%',
              background: 'var(--accent-primary-light)',
              border: '1px solid rgba(59, 130, 246, 0.35)',
              color: 'var(--accent-primary)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <BotIcon size={size === 'sm' ? 14 : size === 'lg' ? 18 : 16} />
          </div>
        )}

        {/* Center Action Label */}
        <span className="truncate" style={{ whiteSpace: 'nowrap' }}>
          {label}
        </span>

        {/* Right Circular Icon Container */}
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: `${iconContainerSize}px`,
            height: `${iconContainerSize}px`,
            borderRadius: '50%',
            background: 'var(--surface-glass-hover)',
            border: 'var(--border-subtle)',
            color: 'var(--accent-cyan)',
            marginLeft: '2px',
            flexShrink: 0,
          }}
        >
          {icon}
        </span>
      </div>
    </button>
  );
};
