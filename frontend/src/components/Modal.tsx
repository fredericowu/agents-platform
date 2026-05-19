import { useEffect, type ReactNode } from "react";

export default function Modal({
  open, onClose, title, children, footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" onClick={onClose}>
      <div className="card w-[min(700px,calc(100vw-32px))] max-h-[calc(100vh-64px)] overflow-y-auto"
           onClick={e => e.stopPropagation()} data-testid="modal">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-fg m-0">{title}</h2>
          <button className="btn" onClick={onClose} aria-label="close" data-testid="modal-close">✕</button>
        </div>
        <div>{children}</div>
        {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}

export function FormRow({ label, hint, children }: {
  label: string; hint?: string; children: ReactNode;
}) {
  return (
    <div className="mb-3">
      <label className="block text-xs text-muted uppercase tracking-wider mb-1">{label}</label>
      {children}
      {hint && <div className="text-xs text-muted mt-1">{hint}</div>}
    </div>
  );
}
