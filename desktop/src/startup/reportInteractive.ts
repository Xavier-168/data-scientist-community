import { useEffect } from 'react';
import { startupBridge } from '../tauri/bridge';

export interface AnimationFrameDriver {
  request(callback: FrameRequestCallback): number;
  cancel(id: number): void;
}

export interface InteractiveReportGate {
  reported: boolean;
}

const GLOBAL_GATE_KEY = '__DATA_SCIENTIST_REACT_INTERACTIVE_GATE__';
const globalWithGate = globalThis as typeof globalThis & {
  __DATA_SCIENTIST_REACT_INTERACTIVE_GATE__?: InteractiveReportGate;
};
const globalGate = (globalWithGate[GLOBAL_GATE_KEY] ??= { reported: false });

const browserAnimationFrames: AnimationFrameDriver = {
  request: (callback) => window.requestAnimationFrame(callback),
  cancel: (id) => window.cancelAnimationFrame(id),
};

const reportThroughBridge = () => startupBridge.markInteractive();

export function useReportInteractive(
  report: () => Promise<void> = reportThroughBridge,
  frames: AnimationFrameDriver = browserAnimationFrames,
  gate: InteractiveReportGate = globalGate,
): void {
  useEffect(() => {
    if (gate.reported) return;

    let active = true;
    let firstFrame: number | undefined;
    let secondFrame: number | undefined;

    firstFrame = frames.request(() => {
      firstFrame = undefined;
      if (!active || gate.reported) return;

      secondFrame = frames.request(() => {
        secondFrame = undefined;
        if (!active || gate.reported) return;

        gate.reported = true;
        void report().catch(() => undefined);
      });
    });

    return () => {
      active = false;
      if (firstFrame !== undefined) frames.cancel(firstFrame);
      if (secondFrame !== undefined) frames.cancel(secondFrame);
    };
  }, [frames, gate, report]);
}
