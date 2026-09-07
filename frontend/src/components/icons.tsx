/** Inline stroke icons. Generic chart-console glyphs, no emoji. */

interface IconProps {
  size?: number;
}

function base(size: number) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
}

export function BrandMark({ size = 21 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.7}>
      <path d="M12 3.2c2.9 2.5 4.6 5.6 5.2 9.3" />
      <path d="M12 3.2c-2.1 2.7-3.2 5.8-3.4 9.3" />
      <path d="M4 16.4c1.7-1.1 3.3-1.1 5 0s3.3 1.1 5 0 3.3-1.1 5 0" />
      <path d="M4 20.2c1.7-1.1 3.3-1.1 5 0s3.3 1.1 5 0 3.3-1.1 5 0" />
    </svg>
  );
}

export function HomeIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-4v-6H9v6H5a1 1 0 0 1-1-1z" />
    </svg>
  );
}

export function NewIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M20 12.5A7.5 7.5 0 0 1 12.5 20H8l-4 3v-4.6A7.5 7.5 0 0 1 8 4.9" />
      <path d="M16 3v6M13 6h6" />
    </svg>
  );
}

export function ChatsIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M8 14H6l-3 2.5V7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      <path d="M10 10h9a2 2 0 0 1 2 2v6.5L18 16h-8a2 2 0 0 1-2-2v-2a2 2 0 0 1 2-2z" />
    </svg>
  );
}

export function SavedIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M6.5 4h11a1 1 0 0 1 1 1v15l-6.5-4-6.5 4V5a1 1 0 0 1 1-1z" />
    </svg>
  );
}

export function BellIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M6 10a6 6 0 1 1 12 0c0 3.4.8 5.2 1.6 6.2a.5.5 0 0 1-.4.8H4.8a.5.5 0 0 1-.4-.8C5.2 15.2 6 13.4 6 10z" />
      <path d="M10 20.2a2.4 2.4 0 0 0 4 0" />
    </svg>
  );
}

export function GearIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.1 14.2a1.6 1.6 0 0 0 .3 1.8l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.6 1.6 0 0 0-2.7 1.1v.3a1.9 1.9 0 1 1-3.8 0v-.2a1.6 1.6 0 0 0-2.8-1.1l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.6 1.6 0 0 0-1.1-2.7h-.3a1.9 1.9 0 0 1 0-3.8h.2a1.6 1.6 0 0 0 1.1-2.8l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1a1.6 1.6 0 0 0 2.7-1.1V3a1.9 1.9 0 1 1 3.8 0v.2a1.6 1.6 0 0 0 2.7 1.1l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.6 1.6 0 0 0 1.1 2.7h.3a1.9 1.9 0 0 1 0 3.8h-.2a1.6 1.6 0 0 0-1.4 1z" />
    </svg>
  );
}

export function BackIcon({ size = 16 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={2}>
      <path d="M19 12H6M11 6l-6 6 6 6" />
    </svg>
  );
}

export function SendIcon({ size = 17 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={2.2}>
      <path d="M5 12h13M13 6l6 6-6 6" />
    </svg>
  );
}

export function MapIcon({ size = 13 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.8}>
      <path d="M9 4 3 6.5v14L9 18l6 2.5 6-2.5v-14L15 6.5z" />
      <path d="M9 4v14M15 6.5v14" />
    </svg>
  );
}

export function DocIcon({ size = 13 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.8}>
      <path d="M6 3h8l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
      <path d="M14 3v4h4M8.5 12.5h7M8.5 16.5h4.5" />
    </svg>
  );
}

export function CloseIcon({ size = 14 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={2}>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  );
}

export function SunIcon({ size = 16 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.8}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

export function MoonIcon({ size = 16 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.8}>
      <path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z" />
    </svg>
  );
}

export function TierIcon({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.7}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c4 4.6 4 12.4 0 17M12 3.5c-4 4.6-4 12.4 0 17" />
    </svg>
  );
}

export function FishIcon({ size = 22 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.5}>
      <ellipse cx="10" cy="12" rx="6" ry="4.2" />
      <path d="M16 12l4.5-3.4v6.8z" />
      <circle cx="7.8" cy="11.6" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function ShieldIcon({ size = 22 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.5}>
      <path d="M12 3l7 3v5c0 5-3.5 8-7 10-3.5-2-7-5-7-10V6z" />
      <path d="m9.5 12 2 2 3.5-4" />
    </svg>
  );
}

export function PinIcon({ size = 22 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.5}>
      <path d="M12 21s7-6.2 7-11.3a7 7 0 1 0-14 0C5 14.8 12 21 12 21z" />
      <circle cx="12" cy="10" r="2.5" />
    </svg>
  );
}

export function WaveIcon({ size = 22 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={1.5}>
      <path d="M3 9c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0M3 15c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0" />
    </svg>
  );
}

export function MenuIcon({ size = 18 }: IconProps) {
  return (
    <svg {...base(size)} strokeWidth={2}>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

export function StormIcon({ size = 19 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M12 3a9 9 0 1 0 9 9" />
      <path d="M12 7a5 5 0 1 0 5 5" />
      <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
    </svg>
  );
}
