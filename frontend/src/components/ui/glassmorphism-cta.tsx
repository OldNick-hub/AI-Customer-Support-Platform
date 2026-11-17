import React, { useState } from "react";
import type { AnchorHTMLAttributes, CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { WandSparkles } from "lucide-react";
import { cn } from "@/lib/utils";

export type GlassmorphismCtaProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  label?: string;
  avatarSrc?: string;
  avatarAlt?: string;
  spread?: string;
  shimmerColor?: string;
  speed?: string;
  size?: "sm" | "md" | "lg";
  to?: string;
};

// Inline SVG AI Copilot Advisor Avatar (Instant offline render, never distorted or broken)
const DefaultAdvisorAvatar: React.FC<{ size: number }> = ({ size }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 32 32"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    className="glass-cta-avatar-img"
    aria-hidden="true"
  >
    <defs>
      <linearGradient id="advisorGrad" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stopColor="#06b6d4" />
        <stop offset="50%" stopColor="#6366f1" />
        <stop offset="100%" stopColor="#a855f7" />
      </linearGradient>
      <linearGradient id="advisorFaceGrad" x1="16" y1="8" x2="16" y2="28" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stopColor="#ffffff" />
        <stop offset="100%" stopColor="#cbd5e1" />
      </linearGradient>
    </defs>
    <circle cx="16" cy="16" r="16" fill="url(#advisorGrad)" />
    <circle cx="16" cy="12.5" r="5" fill="url(#advisorFaceGrad)" />
    <path
      d="M7.5 25.5C8.8 21.5 12.1 19 16 19C19.9 19 23.2 21.5 24.5 25.5C22.2 27.7 19.3 29 16 29C12.7 29 9.8 27.7 7.5 25.5Z"
      fill="url(#advisorFaceGrad)"
    />
  </svg>
);

export default function GlassmorphismCta({
  label = "Generate My Site",
  avatarSrc,
  avatarAlt = "Advisor headshot",
  spread = "90deg",
  shimmerColor = "rgba(255, 255, 255, 0.75)",
  speed = "4s",
  size = "md",
  className,
  href = "#",
  to,
  onClick,
  style,
  ...props
}: GlassmorphismCtaProps) {
  const navigate = useNavigate();
  const targetHref = to ?? href;
  const [imgFailed, setImgFailed] = useState(false);

  const iconSizes = {
    sm: 11,
    md: 13,
    lg: 15,
  };

  const avatarSizes = {
    sm: 24,
    md: 28,
    lg: 32,
  };

  return (
    <a
      href={targetHref}
      onClick={(event) => {
        if (targetHref === "#") {
          event.preventDefault();
        } else if (
          targetHref &&
          targetHref.startsWith("/") &&
          !event.defaultPrevented &&
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.altKey &&
          !event.shiftKey
        ) {
          event.preventDefault();
          navigate(targetHref);
        }
        onClick?.(event);
      }}
      className={cn("glass-cta-root", `glass-cta-size-${size}`, className)}
      style={
        {
          "--spread": spread,
          "--shimmer-color": shimmerColor,
          "--speed": speed,
          ...style,
        } as CSSProperties
      }
      {...props}
    >
      {/* Layer 1: Conic-Gradient Rotating Shimmer Border */}
      <div className="glass-cta-shimmer-track">
        <div className="glass-cta-conic-spin" />
      </div>

      {/* Layer 2: Secondary Traveling Border Beam Highlight */}
      <div className="glass-cta-beam-track">
        <div className="glass-cta-beam-spin" />
      </div>

      {/* Layer 3: Frosted Translucent Glass Core Surface */}
      <div className="glass-cta-surface">
        {/* Specular Light Reflection */}
        <div className="glass-cta-specular" />

        {/* Circular Avatar with Fallback */}
        <div className="glass-cta-avatar-wrap">
          {avatarSrc && !imgFailed ? (
            <img
              src={avatarSrc}
              alt={avatarAlt}
              className="glass-cta-avatar-img"
              onError={() => setImgFailed(true)}
            />
          ) : (
            <DefaultAdvisorAvatar size={avatarSizes[size]} />
          )}
        </div>

        {/* Strictly Single-Line Crisp White Label */}
        <span className="glass-cta-label" title={label}>
          {label}
        </span>

        {/* Proportional Circular WandSparkles Container */}
        <div className="glass-cta-icon-box">
          <WandSparkles
            size={iconSizes[size]}
            strokeWidth={1.75}
            className="text-white"
          />
        </div>
      </div>
    </a>
  );
}

export { GlassmorphismCta };
