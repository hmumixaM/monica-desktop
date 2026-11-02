"use client";

import { flushSync } from "react-dom";

export const PROFILE_AVATAR = "profile-avatar";

type ViewTransition = { ready: Promise<void>; finished: Promise<void>; updateCallbackDone: Promise<void> };
type ViewTransitionDocument = Document & { startViewTransition?: (update: () => Promise<void> | void) => ViewTransition };

export function canAnimate(): boolean {
  return typeof document !== "undefined" && !!(document as ViewTransitionDocument).startViewTransition
    && !matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Polls with timers: the browser pauses rendering, and so animation frames, while a transition's update runs. */
function until(ready: () => boolean, timeout: number): Promise<void> {
  return new Promise((resolve) => {
    const started = performance.now();
    const check = () => (ready() || performance.now() - started > timeout ? resolve() : setTimeout(check, 16));
    check();
  });
}

/** A skipped or timed-out transition still applies its update; only the animation is lost. */
function settle(transition: ViewTransition) {
  for (const promise of [transition.ready, transition.finished, transition.updateCallbackDone]) void promise.catch(() => undefined);
}

/**
 * Navigates inside a view transition. `source` (an avatar) gets the shared name so it morphs into the profile hero;
 * the transition waits briefly for `readySelector` so the new page has something to morph into.
 */
export function transitionTo(navigate: () => void, options: { source?: HTMLElement | null; readySelector?: string; timeout?: number } = {}) {
  if (!canAnimate()) {
    navigate();
    return;
  }
  const { source, readySelector, timeout = 450 } = options;
  if (source) source.style.viewTransitionName = PROFILE_AVATAR;
  const transition = (document as ViewTransitionDocument).startViewTransition!(async () => {
    if (source) source.style.viewTransitionName = "";
    navigate();
    if (readySelector) await until(() => !!document.querySelector(readySelector), timeout);
  });
  settle(transition);
}

/** A same-page state change that cross-fades, such as switching profile tabs. */
export function transitionState(update: () => void) {
  if (!canAnimate()) {
    update();
    return;
  }
  settle((document as ViewTransitionDocument).startViewTransition!(() => flushSync(update)));
}

/** True for clicks the browser should handle itself: new tab, new window, download. */
export function isModifiedClick(event: { metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean; button: number }) {
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
}
