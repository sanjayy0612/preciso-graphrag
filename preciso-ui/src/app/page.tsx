import Navbar from "@/components/Navbar";
import Hero from "@/components/Hero";
import StatsBar from "@/components/StatsBar";
import ComparisonSection from "@/components/ComparisonSection";
import HowItWorks from "@/components/HowItWorks";
import SkillsSection from "@/components/SkillsSection";
import MCPTools from "@/components/MCPTools";
import QuickstartSection from "@/components/QuickstartSection";
import FAQSection from "@/components/FAQSection";
import Footer from "@/components/Footer";

export default function Home() {
  return (
    <>
      <Navbar />
      <main>
        <Hero />
        <StatsBar />
        <ComparisonSection />
        <HowItWorks />
        <SkillsSection />
        <MCPTools />
        <QuickstartSection />
        <FAQSection />
      </main>
      <Footer />
    </>
  );
}
