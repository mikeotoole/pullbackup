// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
//
// Vitest setup, loaded before every test file (see `test.setupFiles` in
// vite.config.ts). Not shipped in the production bundle.

// React checks this global to decide whether `act()` actually does anything.
// Unset, React logs "the current testing environment is not configured to
// support act(...)" and the wrappers silently become no-ops — so a component
// test can assert against a render that has not flushed yet and pass by luck.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export {};
