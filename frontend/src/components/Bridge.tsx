import { str } from "../i18n/strings";
import type { StreamEvent } from "../api/client";
import type { Theme } from "../storage";
import ChartBackdrop from "./ChartBackdrop";
import Composer from "./Composer";
import PipelineTrace from "./PipelineTrace";
import useGlow from "./useGlow";
import Topbar from "./Topbar";
import type { TierSettings } from "../api/client";
import type { FeedState } from "./Topbar";
import { FishIcon, PinIcon, SendIcon, ShieldIcon, WaveIcon } from "./icons";

const PLATES: Record<string, () => JSX.Element> = {
  pfz: () => <FishIcon />,
  safety: () => <ShieldIcon />,
  geofence: () => <PinIcon />,
  conditions: () => <WaveIcon />,
};

interface Props {
  theme: Theme;
  settings: TierSettings | null;
  feed: FeedState;
  busy: boolean;
  /** Live stream events; the compact brewing card renders from these. */
  trace: StreamEvent[];
  onSend: (text: string) => void;
  onOpenSettings: () => void;
  onToggleTheme: () => void;
  onMenu: () => void;
}

const TINTS: Record<string, string> = {
  teal: "var(--teal)",
  green: "var(--green)",
  red: "var(--red)",
  blue: "var(--blue)",
};

/**
 * The landing view: greeting, four task stations, the composer, and the
 * layer chips. Every card and chip sends a REAL English query through the
 * same send() path as typed text -- nothing here is a canned answer.
 */
export default function Bridge({ theme, settings, feed, busy, trace, onSend, onOpenSettings, onToggleTheme, onMenu }: Props) {
  const hero = str.hero;
  const glow = useGlow<HTMLElement>();
  return (
    <main className="canvas" ref={glow.ref}>
      <ChartBackdrop theme={theme} />
      <Topbar settings={settings} feed={feed} theme={theme} onOpenSettings={onOpenSettings} onToggleTheme={onToggleTheme} onMenu={onMenu} />

      <div className="stage">
        <section className="hero">
          <p className="fix">
            {hero.eyebrow.map((seg, i) => (
              <span key={seg} style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                {i > 0 && <span className="sep" aria-hidden="true" />}
                {i === hero.eyebrow.length - 1 ? <em>{seg}</em> : seg}
              </span>
            ))}
          </p>
          <h1>{hero.title}</h1>
          <svg className="hero-wave" viewBox="0 0 300 24" aria-hidden="true">
            <path d="M4 14 C 40 6, 70 6, 104 14 S 170 22, 204 14 S 270 6, 296 14" pathLength={1} />
          </svg>
          <p className="hero-sub">{hero.sub}</p>
        </section>

        <section className="tasks" aria-label={str.tasksNav}>
          {str.tasks.map((task, n) => (
            <button
              key={task.id}
              className="task enter"
              disabled={busy}
              style={{ ["--tint" as string]: TINTS[task.tint] ?? "var(--teal)", animationDelay: `${0.02 + n * 0.04}s` }}
              onClick={() => onSend(task.prompt)}
            >
              <div className="task-head">
                <span className="plate" aria-hidden="true">
                  {(PLATES[task.id] ?? (() => <SendIcon size={16} />))()}
                </span>
                <span className="kind">{task.kind}</span>
              </div>
              <h2>{task.title}</h2>
              <p>{task.body}</p>
              <div className="task-foot">
                <span className="stat">{task.stat}</span>
                <span className="go" aria-hidden="true">
                  <SendIcon size={12} />
                </span>
              </div>
            </button>
          ))}
        </section>

        {busy &&
          (trace.length > 0 ? (
            <div className="deck brew-slot">
              <PipelineTrace events={trace} compact />
            </div>
          ) : (
            <p className="busy-line" role="status">
              <span className="beacon" aria-hidden="true" />
              {str.misc.thinking}
              <span className="dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
            </p>
          ))}
        <section className="deck">
          <Composer busy={busy} onSend={onSend} />
        </section>

        <section className="layers" aria-label="Chart layers">
          {str.chips.map((chip) => (
            <button key={chip.id} className="chip" disabled={busy} onClick={() => onSend(chip.prompt)}>
              {chip.label}
            </button>
          ))}
        </section>
      </div>

      <div className="legend" aria-hidden="true">
        <div>{str.meta.scaleLine}</div>
        <div>{str.meta.datumLine}</div>
      </div>
    </main>
  );
}
