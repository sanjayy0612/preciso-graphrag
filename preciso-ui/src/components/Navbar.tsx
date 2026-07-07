"use client";
import { useState, useEffect } from "react";
import ThemeToggle from "./ThemeToggle";

const NAV_LINKS = [
  { label: "How it works", href: "#how-it-works" },
  { label: "Skills",       href: "#skills" },
  { label: "MCP Tools",    href: "#mcp-tools" },
  { label: "Quickstart",   href: "#quickstart" },
  { label: "FAQ",          href: "#faq" },
  { label: "Visualizer",   href: "/visualizer" },
];

export default function Navbar() {
  const [scrolled, setScrolled] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 border-b-2 border-border ${
        scrolled
          ? "bg-background/90 backdrop-blur-md"
          : "bg-transparent"
      }`}
    >
      <div className="max-w-6xl mx-auto px-6 h-12 flex items-center justify-between gap-6">

        {/* Logo */}
        <a href="#" className="shrink-0 group">
          <span className="font-anton text-[20px] leading-none tracking-tight text-foreground group-hover:text-[var(--red)] transition-colors duration-200 uppercase">
            Preciso
          </span>
        </a>

        {/* Desktop nav — uppercase Barlow Condensed with red hover underline */}
        <nav className="hidden md:flex items-center gap-0">
          {NAV_LINKS.map((link, i) => (
            <a
              key={link.label}
              href={link.href}
              className="relative font-barlow text-sm font-bold uppercase tracking-[0.10em] text-foreground px-3 py-1.5 group transition-colors duration-150"
            >
              {link.label}
              {/* Red underline on hover */}
              <span className="absolute bottom-0 left-3 right-3 h-0.5 bg-[var(--red)] scale-x-0 group-hover:scale-x-100 transition-transform duration-200 origin-left" />
              {/* Separator dot */}
              {i < NAV_LINKS.length - 1 && (
                <span className="absolute right-0 top-1/2 -translate-y-1/2 w-px h-3 bg-foreground/30" />
              )}
            </a>
          ))}
        </nav>

        {/* Right actions */}
        <div className="flex items-center gap-2">
          <ThemeToggle />

          <a
            href="https://github.com/Preciso-GR/preciso-graphrag"
            target="_blank"
            rel="noopener noreferrer"
            className="hidden sm:flex items-center gap-1.5 font-barlow text-sm uppercase tracking-wide text-muted hover:text-foreground transition-colors border-2 border-border hover:border-[var(--red)] px-2.5 py-1"
          >
            <GitHubIcon />
            GitHub
          </a>

          <a
            href="#quickstart"
            className="font-barlow text-sm uppercase tracking-wide font-black bg-[var(--red)] hover:bg-[var(--red-bright)] text-[var(--stripe-text)] px-3 py-1.5 transition-colors duration-150"
          >
            Get Started
          </a>

          {/* Mobile hamburger */}
          <button
            className="md:hidden flex flex-col gap-1.5 p-1"
            onClick={() => setMobileOpen(!mobileOpen)}
            aria-label="Toggle menu"
          >
            <span className={`block w-5 h-0.5 bg-foreground transition-all duration-200 ${mobileOpen ? "rotate-45 translate-y-2" : ""}`} />
            <span className={`block w-5 h-0.5 bg-foreground transition-all duration-200 ${mobileOpen ? "opacity-0" : ""}`} />
            <span className={`block w-5 h-0.5 bg-foreground transition-all duration-200 ${mobileOpen ? "-rotate-45 -translate-y-2" : ""}`} />
          </button>
        </div>
      </div>

      {/* Mobile menu */}
      {mobileOpen && (
        <div className="md:hidden border-t-2 border-border bg-background">
          {NAV_LINKS.map((link) => (
            <a
              key={link.label}
              href={link.href}
              onClick={() => setMobileOpen(false)}
              className="block font-barlow text-sm uppercase tracking-widest text-muted hover:text-foreground hover:bg-surface px-6 py-4 border-b border-border transition-colors"
            >
              {link.label}
            </a>
          ))}
        </div>
      )}
    </header>
  );
}

function GitHubIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" />
    </svg>
  );
}
