// Test-only re-export surface: bundled to tests/.compiled/board-test.mjs by
// `npm run build:board-test` so board-logic.test.mjs can import pure board
// logic under plain Node 20 (no .ts loader, no browser globals from api.ts).
export * from "./boardLogic";
export * from "./boardData";
export { unwrapCandidatesResponse } from "../api";
