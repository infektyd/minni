// Runs the use detector a few times for one session, as a concurrent Stop
// hook would, and prints the statuses (use-detector-single-writer.test.mjs).
import { runDetector } from "../../dist/use-detector.js";

const args = JSON.parse(process.argv[2]);
const rounds = Number(process.argv[3] ?? "5");
const statuses = [];
for (let i = 0; i < rounds; i += 1) statuses.push(runDetector(args).status);
process.stdout.write(JSON.stringify(statuses));
