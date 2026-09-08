import { useEffect, useId, useRef, useState } from "react";
import { str } from "../i18n/strings";
import { SendIcon } from "./icons";
import useGlow from "./useGlow";

interface Props {
  placeholder?: string;
  busy?: boolean;
  compact?: boolean;
  onSend: (text: string) => void;
}

/** The one input. Autogrows, Enter sends, Shift+Enter breaks the line. */
export default function Composer({ placeholder, busy, compact, onSend }: Props) {
  const [value, setValue] = useState("");
  const ta = useRef<HTMLTextAreaElement>(null);
  const fieldId = useId();
  const c = str.composer;
  const glow = useGlow<HTMLFormElement>();

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [value]);

  const filled = value.trim().length > 0;
  const sendable = filled && !busy;

  // The light keeps flowing for as long as there is something in the box,
  // and settles once it is empty again -- including after a send, which
  // clears `value` without a keystroke.
  useEffect(() => {
    glow.fill(filled);
  }, [filled, glow]);

  return (
    <form
      ref={glow.ref}
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        if (!sendable) return;
        onSend(value.trim());
        setValue("");
      }}
    >
      <label
        htmlFor={fieldId}
        style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}
      >
        {placeholder ?? c.placeholder}
      </label>
      <textarea
        ref={ta}
        id={fieldId}
        rows={1}
        value={value}
        disabled={busy}
        placeholder={placeholder ?? c.placeholder}
        onChange={(e) => {
          setValue(e.target.value);
          glow.ping();
        }}
        onPaste={() => glow.ping()}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            if (sendable) {
              onSend(value.trim());
              setValue("");
            }
          }
        }}
      />
      <div className="tools">
        {!compact && (
          <span className="hint">
            <kbd>{c.hintEnter}</kbd> {c.hintToSend} · <kbd>{c.hintShiftEnter}</kbd> {c.hintNewLine}
          </span>
        )}
        {compact && (
          <span className="hint">
            <kbd>{c.hintEnter}</kbd> {c.hintToSend}
          </span>
        )}
        <button
          type="submit"
          className={`send${busy ? " waiting" : ""}`}
          disabled={!sendable}
          aria-label={compact ? c.sendFollowup : c.send}
        >
          <SendIcon />
        </button>
      </div>
    </form>
  );
}
