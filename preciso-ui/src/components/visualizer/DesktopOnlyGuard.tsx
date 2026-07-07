'use client';
import { useState, useEffect } from 'react';
import { SolarSystem } from './SolarSystem';

export function DesktopOnlyGuard({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [isDesktop, setIsDesktop] = useState(false);

  useEffect(() => {
    // Measure viewport on mount and on resize — reading window is only possible
    // client-side, so this state must be synced from an effect.
    /* eslint-disable react-hooks/set-state-in-effect -- syncs to window.innerWidth, a browser-only value unavailable during render */
    const check = () => setIsDesktop(window.innerWidth >= 1024);
    check();
    setReady(true);
    /* eslint-enable react-hooks/set-state-in-effect */
    window.addEventListener('resize', check);
    return () => window.removeEventListener('resize', check);
  }, []);

  if (!ready) return null;
  if (!isDesktop) {
    return (
      <div className="flex-1 flex items-center justify-center p-8 bg-[var(--bg)]">
        <div className="max-w-md text-center">
          <div className="mb-6 flex justify-center">
            <SolarSystem size={160} />
          </div>
          <h2 className="text-lg font-semibold text-[var(--fg)] mb-2 font-mono">
            Open the Visualizer on a desktop
          </h2>
          <p className="text-sm text-[var(--muted)] leading-relaxed">
            The graph workbench needs more room than a mobile screen offers.
            Come back on a laptop or desktop.
          </p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}
