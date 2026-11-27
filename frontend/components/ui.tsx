"use client";

import { type ButtonHTMLAttributes, type ReactNode, useEffect, useRef } from "react";

type Tone = "primary" | "secondary" | "danger" | "ghost";

const TONES: Record<Tone, string> = {
  primary: "bg-blue-700 text-white hover:bg-blue-800 disabled:bg-blue-300",
  secondary:
    "border border-slate-300 bg-white text-slate-800 hover:bg-slate-100 disabled:text-slate-400",
  danger: "bg-red-700 text-white hover:bg-red-800 disabled:bg-red-300",
  ghost: "text-slate-700 hover:bg-slate-100 disabled:text-slate-400",
};

export function Button({
  tone = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone }) {
  return (
    <button
      type="button"
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:cursor-not-allowed ${TONES[tone]} ${className}`}
      {...props}
    />
  );
}

const BADGES: Record<string, string> = {
  active: "bg-blue-50 text-blue-800 ring-blue-200",
  pending: "bg-amber-50 text-amber-900 ring-amber-300",
  generating: "bg-amber-50 text-amber-900 ring-amber-300",
  escalated: "bg-orange-50 text-orange-900 ring-orange-300",
  open: "bg-orange-50 text-orange-900 ring-orange-300",
  executed: "bg-green-50 text-green-800 ring-green-300",
  completed: "bg-green-50 text-green-800 ring-green-300",
  valid: "bg-green-50 text-green-800 ring-green-300",
  resolved: "bg-green-50 text-green-800 ring-green-300",
  rejected: "bg-red-50 text-red-800 ring-red-300",
  failed: "bg-red-50 text-red-800 ring-red-300",
  invalid: "bg-red-50 text-red-800 ring-red-300",
  denied: "bg-red-50 text-red-800 ring-red-300",
  refused: "bg-red-50 text-red-800 ring-red-300",
};

/** A status as text with a tint; the word carries the meaning, never the color alone. */
export function Badge({ value }: { value: string }) {
  const tint = BADGES[value] ?? "bg-slate-100 text-slate-700 ring-slate-300";
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${tint}`}>
      {value.replaceAll("_", " ")}
    </span>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white p-4 shadow-sm ${className}`}>
      {children}
    </section>
  );
}

export function ErrorText({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p role="alert" className="text-sm text-red-700">
      {children}
    </p>
  );
}

/**
 * A modal on the native <dialog>: it traps focus, closes on Escape and returns focus to the
 * element that opened it. On a phone it fills the screen.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-label={title}
      className="m-0 ml-auto h-dvh max-h-dvh w-full max-w-lg overflow-y-auto bg-white p-0 shadow-xl backdrop:bg-slate-900/40"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h2 className="text-lg font-semibold">{title}</h2>
        <Button tone="ghost" onClick={onClose} aria-label="Close">
          ✕
        </Button>
      </div>
      <div className="flex flex-col gap-4 p-4">{children}</div>
    </dialog>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="font-medium text-slate-800">{label}</span>
      {children}
      {hint ? <span className="text-xs text-slate-600">{hint}</span> : null}
    </label>
  );
}

export const inputClass =
  "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-2 focus-visible:outline-blue-700";
