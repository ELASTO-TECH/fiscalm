import { Authentication } from "@/components/Authentication";
import App, { ShellSkeleton } from "@/components/App";

export default function Page() {
  return <Authentication bootstrap pending={<ShellSkeleton />}><App /></Authentication>;
}
