import type { Metadata } from "next";
import { Anton, Barlow_Condensed, Geist, Geist_Mono } from "next/font/google";
import { ThemeProvider } from "@/context/ThemeContext";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });
const anton = Anton({ variable: "--font-anton", subsets: ["latin"], weight: "400" });
const barlowCondensed = Barlow_Condensed({
  variable: "--font-barlow-condensed",
  subsets: ["latin"],
  weight: ["700", "800", "900"],
});

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://preciso.dev";
const TITLE = "Preciso — Precise knowledge graphs from your documents";
const DESCRIPTION =
  "Drop files. Your agent builds a queryable knowledge graph — locally. No cloud, no pipeline, no config. 95/100 benchmark score, zero hallucinations.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: TITLE,
  description: DESCRIPTION,
  keywords: ["GraphRAG", "knowledge graph", "MCP", "RAG", "AI", "local-first"],
  authors: [{ name: "Preciso" }],
  alternates: { canonical: "/" },
  robots: { index: true, follow: true },
  openGraph: {
    title: TITLE,
    description: "Drop files. Agent extracts entities and relationships. Local graph. Zero cloud.",
    type: "website",
    url: SITE_URL,
    siteName: "Preciso",
  },
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: "Drop files. Agent extracts entities and relationships. Local graph. Zero cloud.",
  },
};

// Runs before hydration so the correct theme is applied with no flash of the
// wrong palette. Keep in sync with ThemeContext ("preciso-theme" key, "dark" class).
const THEME_INIT = `(function(){try{var s=localStorage.getItem("preciso-theme");var d=s?s==="dark":matchMedia("(prefers-color-scheme: dark)").matches;if(d)document.documentElement.classList.add("dark");}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} ${anton.variable} ${barlowCondensed.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
