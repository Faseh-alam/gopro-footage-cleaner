/** Stable per-task colors for ScaleAI timeline / task list. */

function hashTask(name: string): number {
  const key = name.trim().toLowerCase();
  let h = 5381;
  for (let i = 0; i < key.length; i += 1) {
    h = Math.imul(h, 33) ^ key.charCodeAt(i);
  }
  return h >>> 0;
}

export function taskColor(task: string | null | undefined): {
  fill: string;
  solid: string;
} {
  const name = String(task || "").trim();
  if (!name) {
    return { fill: "hsla(210, 90%, 55%, 0.35)", solid: "hsl(210, 90%, 55%)" };
  }

  const hash = hashTask(name);
  const hue = ((hash / 0x1_0000_0000) * 360 + 24) % 360;
  const saturation = 72 + ((hash >>> 8) % 17);
  const lightness = 48 + ((hash >>> 16) % 11);
  return {
    fill: `hsla(${hue.toFixed(2)}, ${saturation}%, ${lightness}%, 0.45)`,
    solid: `hsl(${hue.toFixed(2)}, ${saturation}%, ${lightness}%)`,
  };
}
