/** Stable per-task colors for ScaleAI timeline / task list. */

const TASK_PALETTE = [
  { fill: "hsla(210, 90%, 55%, 0.45)", solid: "hsl(210, 90%, 55%)" }, // blue
  { fill: "hsla(145, 70%, 42%, 0.45)", solid: "hsl(145, 70%, 42%)" }, // green
  { fill: "hsla(32, 95%, 52%, 0.45)", solid: "hsl(32, 95%, 52%)" }, // orange
  { fill: "hsla(280, 70%, 58%, 0.45)", solid: "hsl(280, 70%, 58%)" }, // purple
  { fill: "hsla(185, 75%, 42%, 0.45)", solid: "hsl(185, 75%, 42%)" }, // teal
  { fill: "hsla(340, 80%, 55%, 0.45)", solid: "hsl(340, 80%, 55%)" }, // pink
  { fill: "hsla(55, 90%, 48%, 0.45)", solid: "hsl(55, 90%, 45%)" }, // yellow
  { fill: "hsla(0, 75%, 55%, 0.40)", solid: "hsl(0, 75%, 55%)" }, // red-ish
  { fill: "hsla(250, 75%, 62%, 0.45)", solid: "hsl(250, 75%, 62%)" }, // indigo
  { fill: "hsla(165, 60%, 40%, 0.45)", solid: "hsl(165, 60%, 40%)" }, // sea green
  { fill: "hsla(20, 85%, 55%, 0.45)", solid: "hsl(20, 85%, 55%)" }, // coral
  { fill: "hsla(200, 50%, 50%, 0.45)", solid: "hsl(200, 50%, 50%)" }, // steel
] as const;

function hashTask(name: string): number {
  const key = name.trim().toLowerCase();
  let h = 2166136261;
  for (let i = 0; i < key.length; i += 1) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h);
}

export function taskColor(task: string | null | undefined): {
  fill: string;
  solid: string;
} {
  const name = String(task || "").trim();
  if (!name) {
    return { fill: "hsla(210, 90%, 55%, 0.35)", solid: "hsl(210, 90%, 55%)" };
  }
  return TASK_PALETTE[hashTask(name) % TASK_PALETTE.length];
}
