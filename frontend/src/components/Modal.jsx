import { useEffect, useRef, useId } from "react";
import { AlertCircle, CheckCircle, Info, X } from "lucide-react";

/**
 * 通用消息弹窗
 * type: success / error / info
 */
export default function Modal({
  type = "info",
  title,
  children,
  onClose,
  footer,
}) {
  const dialog = useRef(null);
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
      if (e.key === "Tab") {
        const nodes = dialog.current?.querySelectorAll(
          'button:not(:disabled), input, select, textarea, a[href], [tabindex="0"]',
        );
        if (!nodes?.length) {
          e.preventDefault();
          return;
        }
        const first = nodes[0],
          last = nodes[nodes.length - 1];
        if (
          e.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === dialog.current)
        ) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, [onClose]);

  const styles =
    {
      success: {
        icon: <CheckCircle size={20} className="text-green-400" />,
        border: "border-green-500/40",
        iconBg: "bg-green-500/15",
      },
      error: {
        icon: <AlertCircle size={20} className="text-red-400" />,
        border: "border-red-500/40",
        iconBg: "bg-red-500/15",
      },
      info: {
        icon: <Info size={20} className="text-blue-400" />,
        border: "border-blue-500/40",
        iconBg: "bg-blue-500/15",
      },
    }[type] || {};

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
      onClick={onClose}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`card max-w-2xl w-full border ${styles.border} !p-0 overflow-hidden outline-none`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-dark-700">
          <div className="flex items-center gap-2">
            <span className={`p-1.5 rounded-lg ${styles.iconBg}`}>
              {styles.icon}
            </span>
            <h3 id={titleId} className="text-sm font-semibold text-gray-200">
              {title}
            </h3>
          </div>
          <button
            aria-label="关闭弹窗"
            onClick={onClose}
            className="text-gray-400 hover:text-white"
          >
            <X size={16} />
          </button>
        </div>
        <div className="px-4 py-3 text-sm text-gray-300 max-h-[60vh] overflow-y-auto break-all">
          {children}
        </div>
        <div className="px-4 py-3 border-t border-dark-700 flex justify-end gap-2">
          {footer || (
            <button
              onClick={onClose}
              className="btn btn-secondary text-xs px-4 py-1.5"
            >
              关闭
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
