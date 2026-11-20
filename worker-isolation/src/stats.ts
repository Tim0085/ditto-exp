// 进程和容器的资源读数：编排器和被拆出的 Worker 进程都用它。docs\16 阶段三“记录”。
import { readFileSync } from "node:fs";
import { monitorEventLoopDelay, performance } from "node:perf_hooks";

const delay = monitorEventLoopDelay({ resolution: 10 });
delay.enable();

function cgroup(file: string, key?: string): number | null {
  try {
    const text = readFileSync(`/sys/fs/cgroup/${file}`, "utf8");
    if (!key) return Number(text.trim());
    const line = text.split("\n").find(l => l.startsWith(key + " "));
    return line ? Number(line.split(" ")[1]) : null;
  } catch { return null; }
}

/**
 * 整台虚拟机（Docker Desktop 的 Linux 虚拟机）的 CPU：/proc/stat 第一行，[忙的 tick, 全部 tick]；和 1、5、15 分钟的平均负载。
 * 容器里的 /proc/stat、/proc/loadavg 是整个虚拟机的，看得出同一时间有没有别的重活抢 CPU。读不到是 null。
 */
function vmCpu(): [number, number] | null {
  try {
    const f = readFileSync("/proc/stat", "utf8").split("\n")[0]!.trim().split(/\s+/).slice(1).map(Number);
    const total = f.reduce((a, b) => a + b, 0), idle = (f[3] ?? 0) + (f[4] ?? 0);
    return Number.isFinite(total) ? [total - idle, total] : null;
  } catch { return null; }
}
function loadavg(): number[] | null {
  try { return readFileSync("/proc/loadavg", "utf8").trim().split(/\s+/).slice(0, 3).map(Number); } catch { return null; }
}

export interface Snapshot {
  at_ms: number; elu_active_ms: number; elu_idle_ms: number; cpu_ms: number; thread_cpu_ms: number;
  rss_mb: number; heap_mb: number; max_rss_mb: number; cgroup_cpu_ms: number | null; cgroup_mem_mb: number | null; cgroup_mem_peak_mb: number | null;
  vm_cpu_ticks: [number, number] | null; loadavg: number[] | null;
}
export function snapshot(): Snapshot {
  const elu = performance.eventLoopUtilization(), cpu = process.cpuUsage(), thread = process.threadCpuUsage();
  const usec = cgroup("cpu.stat", "usage_usec"), mem = cgroup("memory.current"), peak = cgroup("memory.peak");
  const memory = process.memoryUsage();
  return {
    at_ms: performance.timeOrigin + performance.now(), elu_active_ms: elu.active, elu_idle_ms: elu.idle,
    cpu_ms: (cpu.user + cpu.system) / 1000, thread_cpu_ms: (thread.user + thread.system) / 1000,
    rss_mb: memory.rss / 2 ** 20, heap_mb: memory.heapUsed / 2 ** 20, max_rss_mb: process.resourceUsage().maxRSS / 1024,
    cgroup_cpu_ms: usec === null ? null : usec / 1000, cgroup_mem_mb: mem === null ? null : mem / 2 ** 20,
    cgroup_mem_peak_mb: peak === null ? null : peak / 2 ** 20,
    vm_cpu_ticks: vmCpu(), loadavg: loadavg(),
  };
}
/** 事件循环延迟（毫秒）：被别的工作占住、轮不到的时间。直方图的读数含 10 毫秒的采样间隔，减掉。 */
export function loopDelay() {
  const ms = (ns: number) => Math.max(0, Math.round(ns / 1e4) / 100 - 10);
  return { p50_ms: ms(delay.percentile(50)), p99_ms: ms(delay.percentile(99)), max_ms: ms(delay.max), mean_ms: ms(delay.mean) };
}
export const resetLoopDelay = () => delay.reset();

/** 两次读数之间：主线程忙的时间、CPU、容器 CPU。 */
export function window(a: Snapshot, b: Snapshot) {
  const wall = b.at_ms - a.at_ms, active = b.elu_active_ms - a.elu_active_ms;
  return {
    wall_ms: wall, main_busy_ms: active, main_busy_share: wall > 0 ? active / wall : 0,
    process_cpu_ms: b.cpu_ms - a.cpu_ms, main_thread_cpu_ms: b.thread_cpu_ms - a.thread_cpu_ms,
    container_cpu_ms: a.cgroup_cpu_ms === null || b.cgroup_cpu_ms === null ? null : b.cgroup_cpu_ms - a.cgroup_cpu_ms,
    container_cores_used: a.cgroup_cpu_ms === null || b.cgroup_cpu_ms === null || wall <= 0 ? null : (b.cgroup_cpu_ms - a.cgroup_cpu_ms) / wall,
    rss_end_mb: b.rss_mb, heap_start_mb: a.heap_mb, heap_end_mb: b.heap_mb, max_rss_mb: b.max_rss_mb, container_mem_peak_mb: b.cgroup_mem_peak_mb,
    // 整台虚拟机这段时间里 CPU 忙的比例（含本实验以外的重活）
    vm_cpu_busy_share: a.vm_cpu_ticks && b.vm_cpu_ticks && b.vm_cpu_ticks[1] > a.vm_cpu_ticks[1]
      ? (b.vm_cpu_ticks[0] - a.vm_cpu_ticks[0]) / (b.vm_cpu_ticks[1] - a.vm_cpu_ticks[1]) : null,
  };
}
