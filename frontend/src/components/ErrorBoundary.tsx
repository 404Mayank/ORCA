import { Component, type ReactNode } from "react";

/**
 * Demo insurance: if a panel crashes, show the error in place instead of
 * blanking the entire app. A visible failure can be reported and worked
 * around; a blank page just gets reloaded and the cause is lost.
 */
export default class ErrorBoundary extends Component<
  { children: ReactNode; label: string; onClose?: () => void },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    // eslint-disable-next-line no-console
    console.error(`[orca:${this.props.label}]`, error);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="sheet" role="alert">
        <div className="sheet-head">
          <div>
            <h2>Something broke here</h2>
            <p>
              {this.props.label}: {error.message || String(error)}
            </p>
          </div>
        </div>
        <div className="sheet-body">
          <div className="sheet-note">
            The rest of the app is fine. Close this and keep going -- and tell the team what you
            clicked.
          </div>
          {this.props.onClose && (
            <div className="sheet-note">
              <button className="chip" onClick={this.props.onClose}>
                Close
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }
}
