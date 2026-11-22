import type { Metadata } from "next";
import { AuthView } from "@/components/views/AuthView";

export const metadata: Metadata = { title: "Create an account" };

export default function Page() {
  return <AuthView mode="signup" />;
}
