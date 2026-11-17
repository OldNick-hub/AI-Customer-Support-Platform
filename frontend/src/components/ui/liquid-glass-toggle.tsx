import React, { useState } from 'react';

/**
 * Nimbus UI SVG Icons for LiquidToggle
 */
export const NimbusSunIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  width = 15,
  height = 15,
  style,
  ...props
}) => (
  <svg
    viewBox="0 0 24 24"
    width={width}
    height={height}
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={style}
    {...props}
  >
    <circle cx="12" cy="12" r="4.25" fill="currentColor" fillOpacity="0.22" />
    <path d="M12 2v2.5M12 19.5V22M4.93 4.93l1.77 1.77M17.3 17.3l1.77 1.77M2 12h2.5M19.5 12H22M4.93 19.07l1.77-1.77M17.3 6.7l1.77-1.77" />
  </svg>
);

export const NimbusMoonIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  width = 15,
  height = 15,
  style,
  ...props
}) => (
  <svg
    viewBox="0 0 24 24"
    width={width}
    height={height}
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={style}
    {...props}
  >
    <path
      d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"
      fill="currentColor"
      fillOpacity="0.22"
    />
    <circle cx="16" cy="8" r="0.75" fill="currentColor" stroke="none" />
  </svg>
);

export const NimbusToggleIcons = {
  sun: NimbusSunIcon,
  moon: NimbusMoonIcon,
};

export interface LiquidToggleProps {
  size?: number;
  tone?: 'dark' | 'light';
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  icons?: {
    off?: React.ComponentType<React.SVGProps<SVGSVGElement>>;
    on?: React.ComponentType<React.SVGProps<SVGSVGElement>>;
  };
  label?: string;
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
}

export const LiquidToggle: React.FC<LiquidToggleProps> = ({
  size = 58,
  tone,
  checked,
  onCheckedChange,
  icons = { off: NimbusToggleIcons.sun, on: NimbusToggleIcons.moon },
  label,
  disabled = false,
  className = '',
  style,
}) => {
  const [isPressing, setIsPressing] = useState(false);

  // Derive active tone from prop or checked state
  const activeTone = tone ?? (checked ? 'dark' : 'light');
  const isDarkTone = activeTone === 'dark';

  // Dynamic geometry based on size
  const trackWidth = size;
  const trackHeight = Math.round(size * 0.54);
  const padding = 3;
  const thumbSize = trackHeight - padding * 2;
  const travelDistance = trackWidth - thumbSize - padding * 2;
  const iconSize = Math.max(12, Math.round(thumbSize * 0.58));
  const ghostIconSize = Math.max(10, Math.round(thumbSize * 0.48));

  const OffIcon = icons.off ?? NimbusToggleIcons.sun;
  const OnIcon = icons.on ?? NimbusToggleIcons.moon;

  const handleToggle = () => {
    if (disabled) return;
    onCheckedChange(!checked);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      onCheckedChange(!checked);
    }
  };

  return (
    <div
      role="switch"
      tabIndex={disabled ? -1 : 0}
      aria-checked={checked}
      aria-label={label || (checked ? 'Dark mode enabled. Click to switch to Light mode' : 'Light mode enabled. Click to switch to Dark mode')}
      title={label || (checked ? 'Switch to Light Mode' : 'Switch to Dark Mode')}
      className={`liquid-toggle-root ${className}`}
      onClick={handleToggle}
      onKeyDown={handleKeyDown}
      onMouseDown={() => !disabled && setIsPressing(true)}
      onMouseUp={() => setIsPressing(false)}
      onMouseLeave={() => setIsPressing(false)}
      style={{
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        width: `${trackWidth}px`,
        height: `${trackHeight}px`,
        borderRadius: '9999px',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        userSelect: 'none',
        WebkitTapHighlightColor: 'transparent',
        outline: 'none',
        transition: 'all 0.3s ease',
        ...style,
      }}
    >
      {/* Liquid Glass Track */}
      <div
        className="liquid-toggle-track"
        style={{
          position: 'absolute',
          inset: 0,
          borderRadius: '9999px',
          background: isDarkTone
            ? 'linear-gradient(135deg, rgba(255, 255, 255, 0.08) 0%, rgba(15, 23, 42, 0.65) 100%)'
            : 'linear-gradient(135deg, rgba(255, 255, 255, 0.88) 0%, rgba(220, 235, 245, 0.72) 100%)',
          backdropFilter: 'blur(16px) saturate(180%)',
          WebkitBackdropFilter: 'blur(16px) saturate(180%)',
          border: isDarkTone
            ? '1px solid rgba(255, 255, 255, 0.16)'
            : '1px solid rgba(0, 0, 0, 0.08)',
          boxShadow: isDarkTone
            ? 'inset 0 2px 4px rgba(0, 0, 0, 0.45), inset 0 -1px 2px rgba(255, 255, 255, 0.06), 0 3px 10px rgba(0, 0, 0, 0.25)'
            : 'inset 0 2px 4px rgba(0, 0, 0, 0.06), inset 0 -1px 2px rgba(255, 255, 255, 0.95), 0 3px 10px rgba(0, 0, 0, 0.06)',
          overflow: 'hidden',
          transition: 'all 0.45s cubic-bezier(0.16, 1, 0.3, 1)',
        }}
      >
        {/* Top Rim Specular Reflection */}
        <div
          style={{
            position: 'absolute',
            top: '1px',
            left: '8px',
            right: '8px',
            height: '1px',
            background: isDarkTone
              ? 'linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.35), transparent)'
              : 'linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.9), transparent)',
            borderRadius: '9999px',
            pointerEvents: 'none',
          }}
        />

        {/* Ambient Track Glow / Wash */}
        <div
          style={{
            position: 'absolute',
            inset: 0,
            borderRadius: '9999px',
            background: checked
              ? 'radial-gradient(circle at 75% 50%, rgba(56, 189, 248, 0.15) 0%, transparent 70%)'
              : 'radial-gradient(circle at 25% 50%, rgba(245, 158, 11, 0.15) 0%, transparent 70%)',
            transition: 'background 0.5s ease',
            pointerEvents: 'none',
          }}
        />

        {/* Faint Background Track Guides (Sun left, Moon right) */}
        <div
          style={{
            position: 'absolute',
            left: `${padding + 3}px`,
            top: '50%',
            transform: 'translateY(-50%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: isDarkTone ? 'rgba(255, 255, 255, 0.2)' : 'rgba(245, 158, 11, 0.35)',
            opacity: checked ? 0.45 : 0,
            transition: 'opacity 0.3s ease',
            pointerEvents: 'none',
          }}
        >
          <OffIcon width={ghostIconSize} height={ghostIconSize} />
        </div>

        <div
          style={{
            position: 'absolute',
            right: `${padding + 3}px`,
            top: '50%',
            transform: 'translateY(-50%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: isDarkTone ? 'rgba(147, 197, 253, 0.4)' : 'rgba(11, 19, 43, 0.2)',
            opacity: !checked ? 0.45 : 0,
            transition: 'opacity 0.3s ease',
            pointerEvents: 'none',
          }}
        >
          <OnIcon width={ghostIconSize} height={ghostIconSize} />
        </div>
      </div>

      {/* Fluid Sliding Droplet / Thumb */}
      <div
        className="liquid-toggle-thumb"
        style={{
          position: 'absolute',
          top: `${padding}px`,
          left: `${padding}px`,
          width: `${thumbSize}px`,
          height: `${thumbSize}px`,
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          transform: `translateX(${checked ? travelDistance : 0}px) scaleX(${
            isPressing ? 1.15 : 1
          }) scaleY(${isPressing ? 0.92 : 1})`,
          transformOrigin: checked ? 'right center' : 'left center',
          background: isDarkTone
            ? 'linear-gradient(145deg, #1E293B 0%, #0F172A 100%)'
            : 'linear-gradient(145deg, #FFFFFF 0%, #F1F5F9 100%)',
          border: isDarkTone
            ? '1px solid rgba(147, 197, 253, 0.35)'
            : '1px solid rgba(255, 255, 255, 0.95)',
          boxShadow: isDarkTone
            ? '0 3px 10px rgba(0, 0, 0, 0.5), inset 0 1px 1px rgba(255, 255, 255, 0.4), inset 0 -1px 2px rgba(0, 0, 0, 0.6), 0 0 14px rgba(56, 189, 248, 0.35)'
            : '0 3px 10px rgba(0, 0, 0, 0.12), inset 0 1px 2px #FFFFFF, inset 0 -1px 2px rgba(0, 0, 0, 0.04), 0 0 12px rgba(245, 158, 11, 0.35)',
          transition:
            'transform 0.45s cubic-bezier(0.34, 1.45, 0.64, 1), background 0.35s ease, box-shadow 0.35s ease, border-color 0.35s ease',
          zIndex: 2,
        }}
      >
        {/* Specular Droplet Curved Glint */}
        <div
          style={{
            position: 'absolute',
            top: '2px',
            left: '4px',
            width: '58%',
            height: '34%',
            borderRadius: '50%',
            background: 'linear-gradient(180deg, rgba(255, 255, 255, 0.75) 0%, rgba(255, 255, 255, 0) 100%)',
            pointerEvents: 'none',
          }}
        />

        {/* Dynamic Center Icon with Fluid Rotation and Scale Transition */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            transform: `rotate(${checked ? '0deg' : '-180deg'}) scale(${isPressing ? 0.9 : 1})`,
            transition: 'transform 0.45s cubic-bezier(0.34, 1.45, 0.64, 1), color 0.3s ease, filter 0.3s ease',
            color: checked ? '#93C5FD' : '#D97706',
            filter: checked
              ? 'drop-shadow(0 0 4px rgba(147, 197, 253, 0.7))'
              : 'drop-shadow(0 0 4px rgba(245, 158, 11, 0.6))',
          }}
        >
          {checked ? (
            <OnIcon width={iconSize} height={iconSize} />
          ) : (
            <OffIcon width={iconSize} height={iconSize} />
          )}
        </div>
      </div>
    </div>
  );
};

