import type { ToastMessage } from "@/types";

interface ToastProps {
  toast: ToastMessage | null;
}

export function Toast({ toast }: ToastProps) {
  if (!toast) return null;

  return (
    <div
      className={`fixed bottom-4 right-4 bg-[var(--bg-header)] border rounded px-3 py-2 text-[11px] z-[100] flex items-center gap-2 animate-[toast-slide-in_0.25s_ease-out] ${
        toast.type === "success" ? "border-[var(--color-up)]" : "border-[var(--color-down)]"
      }`}
      style={{ boxShadow: "0 10px 15px -3px rgba(0,0,0,0.5)" }}
    >
      <span
        className={`w-2 h-2 rounded-full ${toast.type === "success" ? "bg-[var(--color-up)]" : "bg-[var(--color-down)]"}`}
      />
      <span className="text-[var(--text-primary)]">{toast.text}</span>
    </div>
  );
}
