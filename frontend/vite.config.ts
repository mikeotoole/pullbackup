// `defineConfig` is imported from vitest/config rather than vite so the `test`
// block typechecks. `tsc -b` covers this file, and vite's own UserConfig has no
// `test` key.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    // The default environment stays `node`. Only the component suites need a
    // DOM, and they opt in with a `@vitest-environment jsdom` docblock —
    // `environmentMatchGlobs` was removed in Vitest 4.
    //
    // Making jsdom the global default was tried and is WRONG here: under jsdom
    // `import.meta.url` is an http URL, so src/lib/packageLockPortability.test.js
    // fails at import with "The URL must be of scheme file", and the
    // static-markup suites start emitting useLayoutEffect SSR warnings.
    //
    // React only honours `act()` — flushing effects and state updates
    // synchronously — when `IS_REACT_ACT_ENVIRONMENT` is set. Without it React
    // warns "the current testing environment is not configured to support
    // act(...)" and the act() wrappers become no-ops, so an assertion can run
    // against a render that has not happened yet. Set in a setup FILE rather
    // than via `define`, which would bake the flag into the production bundle.
    setupFiles: ["./src/test-setup.ts"],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
