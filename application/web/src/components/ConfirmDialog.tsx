import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";

export type ConfirmOptions = {
  title: string;
  message: string;
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  dontAskAgainKey?: string;
};

type Props = {
  open: boolean;
  options: ConfirmOptions | null;
  onConfirm: (dontAskAgain: boolean) => void;
  onCancel: () => void;
};

export function ConfirmDialog({ open, options, onConfirm, onCancel }: Props) {
  const titleId = useId();
  const [dontAsk, setDontAsk] = useState(false);

  useEffect(() => {
    if (open) setDontAsk(false);
  }, [open, options?.title, options?.message]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      } else if (e.key === "Enter") {
        e.preventDefault();
        onConfirm(dontAsk);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dontAsk, onCancel, onConfirm]);

  if (!open || !options) return null;

  const confirmLabel = options.confirmLabel ?? "Confirm";
  const cancelLabel = options.cancelLabel ?? "Cancel";

  return createPortal(
    <div
      className="modal-overlay confirm-dialog-overlay"
      role="presentation"
      onMouseDown={onCancel}
    >
      <div
        className="modal confirm-dialog-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id={titleId} className="modal-title">
            {options.title}
          </h2>
          <button type="button" className="modal-close" aria-label="Close" onClick={onCancel}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>

        <div className="modal-body">
          <p className="modal-message">{options.message}</p>
          {options.detail ? <p className="modal-detail">{options.detail}</p> : null}
        </div>

        <div className="modal-footer">
          {options.dontAskAgainKey ? (
            <label className="modal-dont-ask">
              <input
                type="checkbox"
                checked={dontAsk}
                onChange={(e) => setDontAsk(e.target.checked)}
              />
              <span>다시 묻지 않기</span>
            </label>
          ) : (
            <span />
          )}
          <div className="modal-actions">
            <button type="button" className="modal-btn-secondary" onClick={onCancel}>
              {cancelLabel}
            </button>
            <button
              type="button"
              className={`send-btn${options.danger ? " confirm-btn-danger" : ""}`}
              onClick={() => onConfirm(dontAsk)}
            >
              {confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
