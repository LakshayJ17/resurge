import type { Metadata } from "next";
import { AuthView } from "@/components/views/AuthView";

export const metadata: Metadata = { title: "Log in" };

export default function Page() {
  return <AuthView mode="login" />;
}
