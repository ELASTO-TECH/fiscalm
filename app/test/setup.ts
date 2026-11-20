import "@testing-library/jest-dom/vitest";
import axe from "axe-core";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom has no layout; contrast is measured separately in contrast.test.ts and the browser.
axe.configure({ rules: [{ id: "color-contrast", enabled: false }] });
HTMLCanvasElement.prototype.getContext = () => null;

// findBy/waitFor wait up to 4 s: on a loaded machine a redirect chain (read ALL, pick a company, read it) can take
// longer than the 1 s default, and the tests must not depend on the machine's load.
configure({ asyncUtilTimeout: 4000 });

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

// jsdom has no layout: scrolling is a no-op.
Element.prototype.scrollIntoView = function scrollIntoView() {};
window.scrollTo = () => {};
