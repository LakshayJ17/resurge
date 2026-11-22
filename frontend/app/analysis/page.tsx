import type { Metadata } from "next";
import { AnalysisView } from "@/components/views/AnalysisView";

export const metadata: Metadata = { title: "Analysis" };

export default function Page() {
  return <AnalysisView />;
}
