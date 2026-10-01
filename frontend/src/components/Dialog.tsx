import { useEffect, useRef, type ReactNode } from "react";

// A native <dialog> (focus trap, Esc to close, top layer) driven by React state.
export function Dialog({
  open,
  onClose,
  labelledBy,
  wide = false,
  children,
}: {
  open: boolean;
  onClose: () => void;
  labelledBy: string;
  wide?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className="hud-dialog"
      style={wide ? { width: "min(820px, calc(100vw - 32px))" } : undefined}
      aria-labelledby={labelledBy}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      {open ? children : null}
    </dialog>
  );
}
