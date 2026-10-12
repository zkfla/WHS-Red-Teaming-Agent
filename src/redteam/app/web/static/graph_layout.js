// Layout is presentation only; connections always come from the server's edge list.
export const NODE_WIDTH = 238;
export const NODE_HEIGHT = 112;
export const COLUMNS = [90, 440, 790, 1140];

export function graphLayout(view) {
  const positions = new Map();
  const tasks = view.tasks;
  const workers = view.workers;
  let y = 122;
  for (const task of tasks) {
    const mine = workers.filter((worker) => worker.task_id === task.task_id);
    const block = Math.max(1, mine.length) * 158;
    positions.set(`task:${task.task_id}`, { x: COLUMNS[1], y: y + (block - 158) / 2 });
    mine.forEach((worker, index) => positions.set(`worker:${worker.instance_id}`, { x: COLUMNS[2], y: y + index * 158 }));
    y += block;
  }
  // Retained workers can outlive the task list. Keep them visible without inventing links.
  for (const worker of workers) {
    const id = `worker:${worker.instance_id}`;
    if (!positions.has(id)) {
      positions.set(id, { x: COLUMNS[2], y });
      y += 158;
    }
  }
  const height = Math.max(490, y + 118);
  const center = (122 + Math.max(122, y - 158)) / 2;
  positions.set('planner', { x: COLUMNS[0], y: center });
  positions.set('origin', { x: COLUMNS[3], y: center });
  return { positions, width: 1470, height };
}

export function connectedPath(edges, selected) {
  if (!selected) return null;
  const related = new Set([selected]);
  const flow = edges.filter((edge) => edge.kind !== 'wakes');
  for (const direction of ['forward', 'backward']) {
    const reached = new Set([selected]);
    const queue = [selected];
    while (queue.length) {
      const id = queue.shift();
      for (const edge of flow) {
        const from = direction === 'forward' ? edge.from : edge.to;
        const to = direction === 'forward' ? edge.to : edge.from;
        if (from === id && !reached.has(to)) {
          reached.add(to);
          related.add(to);
          queue.push(to);
        }
      }
    }
  }
  return related;
}
