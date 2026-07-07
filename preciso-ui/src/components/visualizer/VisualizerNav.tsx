'use client';
import { useState, useEffect } from 'react';
import Link from 'next/link';
import ThemeToggle from '@/components/ThemeToggle';

const NAV_LINKS = [
  { label: 'Docs',  href: 'https://github.com/Preciso-GR/preciso-graphrag#readme' },
  { label: 'About', href: '/' },
];

function StarIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
    </svg>
  );
}

function GitHubIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" />
    </svg>
  );
}

export function VisualizerNav() {
  const [stars, setStars] = useState<number | null>(null);

  useEffect(() => {
    fetch('https://api.github.com/repos/Preciso-GR/preciso-graphrag')
      .then(r => r.json())
      .then(d => typeof d.stargazers_count === 'number' && setStars(d.stargazers_count))
      .catch(() => {});
  }, []);

  return (
    <header className="fixed top-0 left-0 right-0 z-50 border-b-2 border-border bg-background/90 backdrop-blur-md">
      <div className="px-5 h-12 flex items-center gap-6">
        {/* Logo */}
        <Link href="/" className="shrink-0 group mr-2">
          <span className="font-anton text-[18px] leading-none tracking-tight text-foreground group-hover:text-[var(--red)] transition-colors uppercase">
            Preciso
          </span>
        </Link>

        {/* Tool label */}
        <span className="font-mono text-xs text-muted border border-border px-2 py-0.5">
          Visualizer
        </span>

        {/* Nav links */}
        <nav className="flex items-center gap-5 ml-2">
          {NAV_LINKS.map(link => {
            const cls = "font-mono text-xs text-muted hover:text-foreground transition-colors";
            return link.href.startsWith("/") ? (
              <Link key={link.label} href={link.href} className={cls}>
                {link.label}
              </Link>
            ) : (
              <a key={link.label} href={link.href} target="_blank" rel="noopener noreferrer" className={cls}>
                {link.label}
              </a>
            );
          })}
        </nav>

        <div className="flex-1" />

        {/* Right: theme toggle + GitHub */}
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <a
            href="https://github.com/Preciso-GR/preciso-graphrag"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 font-mono text-xs text-muted hover:text-foreground transition-colors border border-border hover:border-foreground px-2.5 py-1.5"
          >
            <GitHubIcon />
            {stars !== null ? (
              <span className="flex items-center gap-1">
                <StarIcon />
                {stars}
              </span>
            ) : (
              'GitHub'
            )}
          </a>
        </div>
      </div>
    </header>
  );
}
