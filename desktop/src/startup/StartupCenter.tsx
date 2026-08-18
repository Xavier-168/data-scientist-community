import type { StartupState } from './startupTypes';

interface StartupCenterProps {
  state: StartupState;
  onRetry: () => void;
  onOpenLogs: () => void;
  onOpenConsole: () => void;
}

export function StartupCenter({ state, onRetry, onOpenLogs, onOpenConsole }: StartupCenterProps) {
  const percent = Math.round(state.progress * 100);

  return (
    <main className="startup-center" data-phase={state.phase}>
      <div className="startup-shell">
        <header className="product-header">
          <span className="product-mark" aria-hidden="true">
            <span>D</span>
          </span>
          <div className="product-identity">
            <p className="product-kicker">LOCAL DATA INSTRUMENT</p>
            <h1>数据科学家 Community</h1>
            <p className="product-summary">控制台已经打开，采集引擎正在后台准备</p>
          </div>
          <div className="response-state" aria-label="控制台已响应">
            <span aria-hidden="true" />
            控制台已响应
          </div>
        </header>

        <section className="startup-card" aria-label="启动状态">
          <div className="instrument-scale" aria-hidden="true">
            <span>00</span>
            <span>25</span>
            <span>50</span>
            <span>75</span>
            <span>100</span>
          </div>

          <div className="startup-reading">
            <div>
              <p className="reading-label">当前阶段</p>
              <p className="startup-message" role="status" aria-live="polite" aria-atomic="true">
                {state.message}
              </p>
            </div>
            <output className="startup-percent" aria-label={`启动进度 ${percent}%`}>
              {percent}
              <span>%</span>
            </output>
          </div>

          <progress
            max={100}
            value={percent}
            aria-label="启动进度"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
          />

          <dl className="startup-statuses">
            <div className="status-item" data-ready={state.core_ready}>
              <dt>核心服务</dt>
              <dd>
                <span className="status-dot" aria-hidden="true" />
                {state.core_ready ? '已就绪' : '准备中'}
              </dd>
            </div>
            <div className="status-item" data-ready={state.collector_ready}>
              <dt>采集引擎</dt>
              <dd>
                <span className="status-dot" aria-hidden="true" />
                {state.collector_ready ? '已就绪' : '后台准备'}
              </dd>
            </div>
            <div className="status-item" data-ready={state.app_installed}>
              <dt>应用安装</dt>
              <dd>
                <span className="status-dot" aria-hidden="true" />
                {state.app_installed ? '已完成' : '后台进行'}
              </dd>
            </div>
          </dl>

          <footer className="startup-footer">
            <p>准备期间界面保持可交互</p>
            <div className="startup-actions">
              <button className="button button-secondary" type="button" onClick={onOpenLogs}>
                查看启动日志
              </button>
              <button className="button button-primary" type="button" onClick={onOpenConsole}>
                打开控制台
              </button>
              {state.can_retry && (
                <button className="button button-primary" type="button" onClick={onRetry}>
                  重新准备
                </button>
              )}
            </div>
          </footer>
        </section>
      </div>
    </main>
  );
}
