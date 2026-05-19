import type { ReactNode } from "react";

export default function Page({
  title, subtitle, actions, children,
}: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="p-6 max-w-[1400px] mx-auto">
      <header className="flex items-end justify-between border-b border-line pb-4 mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-accent m-0">{title}</h1>
          {subtitle && <div className="text-sm text-muted mt-1">{subtitle}</div>}
        </div>
        <div className="flex items-center gap-2">{actions}</div>
      </header>
      {children}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const cls = {
    success: "badge-success",
    error: "badge-error",
    running: "badge-running",
    pending: "badge-pending",
    cancelled: "badge-warn",
  }[status as string] ?? "badge";
  return <span className={`badge ${cls}`}>{status}</span>;
}
