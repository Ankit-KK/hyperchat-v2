import { vitestPreset } from "@hyperchat/config/vitest";

export default vitestPreset({ environment: "jsdom", setupFiles: ["./src/test-setup.ts"] });
