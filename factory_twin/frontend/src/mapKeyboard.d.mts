export const MAP_KEYBOARD_HINT: string;
export function createMapKeyboard(options: {
  begin: (key: string) => { move: (step: number, key: string) => void; finish: () => void } | undefined;
  now?: () => number;
  requestFrame?: (callback: FrameRequestCallback) => number;
  cancelFrame?: (id: number) => void;
}): { down: (event: KeyboardEvent) => void; up: (event: KeyboardEvent) => void; stop: () => void };
