// 每次调用的原始用时（docs\31：全量实验的记录要全面，后面不再重跑）。编排器、被拆出的 Worker 进程、Direct 都用它。
//
// 一行 [节点编号, 开始时刻, 用时, 题的序号, 类别] 或再加第 6 项 [..., 执行标识]：
// - 节点编号：nodes 数组里的下标（"CONTEXT.UPDATE" 这样的节点名）；
// - 开始时刻：Unix 毫秒（performance.timeOrigin + performance.now()），不同进程可以对齐，保留 0.01 毫秒；用时：毫秒，保留 0.001；
// - 题的序号：编排器里由 AsyncLocalStorage 认出是哪一题（run.ts 的 runOne 里设）；被拆出的进程认不出，记 -1；
// - 类别：0 = 在本进程里执行（Worker 的执行入口，或 Direct 的直接调用），1 = 编排器经官方 HTTP 传输调远端的往返；
// - 执行标识：只有跨进程的调用才记——Ditto 运行时给图里每个节点的 runId（取前 12 位十六进制）和 nodeId，
//   编排器的往返和被拆出进程的执行记的是同一个，据此把远端的每次执行对回到题。
// 记一次只多两次读时钟和一次数组追加，各布局相同。
import { AsyncLocalStorage } from "node:async_hooks";
import { performance } from "node:perf_hooks";
import type { WorkerDefinition } from "@codesoul-co/ditto/worker";

export const jobContext = new AsyncLocalStorage<number>();
export const LOCAL = 0, REMOTE = 1;

export interface CallsJson { nodes: string[]; rows: (number | string)[][] }

/** Ditto 运行时的执行范围（runGraph 给每个节点的 { graphId, runId, nodeId }）→ 执行标识；没有就是 undefined。 */
export function execKey(scope: unknown): string | undefined {
  const s = scope as { runId?: unknown; nodeId?: unknown } | undefined;
  return typeof s?.runId === "string" && typeof s.nodeId === "string" ? `${s.runId.replaceAll("-", "").slice(0, 12)}/${s.nodeId}` : undefined;
}

export class CallLog {
  private readonly ids = new Map<string, number>();
  readonly rows: (number | string)[][] = [];

  record(node: string, t0: number, ms: number, kind = LOCAL, exec?: string) {
    let id = this.ids.get(node);
    if (id === undefined) this.ids.set(node, id = this.ids.size);
    const row: (number | string)[] = [id, Math.round((performance.timeOrigin + t0) * 100) / 100, Math.round(ms * 1000) / 1000, jobContext.getStore() ?? -1, kind];
    if (exec !== undefined) row.push(exec);
    this.rows.push(row);
  }

  /** 给一次调用计时（出错也记）。 */
  async time<T>(node: string, fn: () => Promise<T> | T, kind = LOCAL, exec?: string): Promise<T> {
    const t0 = performance.now();
    try { return await fn(); } finally { this.record(node, t0, performance.now() - t0, kind, exec); }
  }

  /** 稳态窗口 [from, to]（Unix 毫秒）里结束的调用，按节点、类别汇总（run.ts 写进结果的 nodeWalls）。 */
  summary(from: number, to: number, kind: number) {
    const names = [...this.ids.keys()];
    const by = new Map<string, number[]>();
    for (const [id, start, ms, , k] of this.rows as number[][]) {
      if (k !== kind || start! + ms! < from || start! + ms! > to) continue;
      const name = names[id!]!;
      let xs = by.get(name); if (!xs) by.set(name, xs = []); xs.push(ms!);
    }
    return by;
  }

  toJSON(): CallsJson { return { nodes: [...this.ids.keys()], rows: this.rows }; }
}

/**
 * 给 Worker 的执行入口套一层计时，不改节点本身（被拆出的进程用；编排器另有 run.ts 的 instrument，阶段一还要量别的）。
 * 记下执行标识（运行时交给 Worker 的 services.execution），和编排器那边的往返对得上。
 */
export function timedWorker(def: WorkerDefinition, log: CallLog): WorkerDefinition {
  return {
    type: def.type, capabilities: def.capabilities, ...(def.nodeTypes ? { nodeTypes: def.nodeTypes } : {}),
    ...(def.concurrency === undefined ? {} : { concurrency: def.concurrency }),
    instantiate() {
      const inner = def.instantiate();
      return {
        execute: (node, input, services) => log.time(node, () => inner.execute(node, input, services), LOCAL,
          execKey((services as { execution?: unknown } | undefined)?.execution)),
        ...(inner.dispose ? { dispose: () => inner.dispose!() } : {}),
      };
    },
  };
}
