// 真实模型的出口（docs\19 第 3 节）。
//
// 只有这个进程能上外网、拿到 DeepSeek 密钥；编排器和 MEMORY 容器都在不能出外网的网络里，模型请求都发到这里。
// 对 Ditto 来说它和假模型一样：OpenAI chat completions 接口，外加 /reset、/stats 两个控制入口，run.ts 不用改。
//
// 每个请求：
//  1. 检查：模型名必须是 deepseek-flash，不能是流式、不能带工具；最后一条用户消息开头必须有内部标记 [task:题号][step:keyword|answer]。
//     官方 rag-qa（docs\24）例外：消息是官方 inferGraph 原样发来的，不能加标记。正好两条消息（system、user），
//     system 里有官方 4 步提示词之一的独有句子、user 里认得出问题，才放行；题记成问题文字的哈希、步骤记成 plan/select/answer/verify
//     （rag-fake.ts 的 ragKind、ragTask）。同一格里同一题同一步来第二次（官方回答、核对不合格时重来一次），步骤记成 answer#2 这样。
//  2. 记下 Ditto 发来的原始请求体的 SHA-256（比较两种布局发出的请求是否相同）。然后去掉标记，在第一条消息开头加一行
//     “Request ID: <16 位哈希>”（由格子、题号、步骤算出）：DeepSeek 按请求开头相同的部分做缓存，每格每题开头都不同，
//     后跑的格子就不会因为命中前面格子的缓存而更便宜、更快（对先跑的布局不公平）。只放不透明、等长的哈希：
//     题号里的 “_abs” 和格子名里的布局名不能让模型看见。模型实际看到的是：这一行 + 原来的提示词。
//  3. 统一加上 thinking: disabled（关闭思考），只转发 messages、max_tokens 和采样参数（temperature、top_p、stop、seed）。
//  4. 发之前按最坏情况预扣费用：输入 token 数不超过消息的字节数，全按没命中缓存的价格；输出按 max_tokens。超过上限就不发。
//  5. 收到回复按 usage 结算（缓存命中、没命中、输出分别计价）。费用不明的（发出去以后断线、超时，或回复里没有 usage）
//     保留预扣、计入已花，不清零。
//  6. 临时故障（429、5xx、连接断开、DeepSeek 推理资源不足）最多重试 2 次，退避 5、15 秒；单次超时 90 秒，超时的不重试
//     （DeepSeek 多半还在算、还会收费）；一个请求从收到起 285 秒截止（Ditto 那边等回复头最多 300 秒）。
//     401、403、402（鉴权、余额）立刻停；连续 20 个请求最终失败也停。
//     停了以后新请求、/reset 都返回 503，下一格不会开始。
// 账本逐笔追加写到 --ledger 目录（这个容器不在被测的核上，同步写不影响测量）；重启时重放，上限跨重启累计。
// 同一目录另记两份完整记录（docs\31：全量实验的记录要全面，后面不再重跑）：
//   bodies.jsonl     Ditto 发来的原始请求体全文，按 SHA-256 去重（同一个请求体只记一次；bodies.index 是已记过的摘要，重启时据此接着去重）。
//                    发给 DeepSeek 的内容 = 这个请求体去掉内部标记、system 开头加 “Request ID: <requests.jsonl 里的 rid>”、加上 thinking 关闭。
//                    在回复 Ditto 以后才写（不占这个请求的用时）。
//   responses.jsonl  DeepSeek 每次尝试的完整回复（含超时以后才到的，late=true），按 requests.jsonl 的 id 加 “#第几次尝试” 对应。
//                    正常回复原样记（只把真实密钥换掉，万一出现的话）；错误回复按打码规则记。
//   resets.jsonl     每格复位时的格子名和随机数：rid = sha256(随机数|格子|题|步骤) 的前 16 位，请求中途进程没了也能还原。
// 请求编号是 “格子/题/步骤/随机数前 8 位.序号”：出口重启、同一格重跑，编号也不会重复。
// 密钥只在内存里，不写文件、不进日志；写出去的任何文字里像 sk-xxx 的串都打码。
// 认密钥用的前缀（sk- 后面再加几位）从环境变量 DEEPSEEK_KEY_PREFIX 读，写在不进 git 的本地配置 local.env 里（real.sh 读进来），代码里不写。
//
//   node src/egress.ts --port 8080 --key-file /run/secrets/deepseek.key --ledger /ledger --limit-cny 400
//   node src/egress.ts --check-key <文件>     只检查文件里有没有这个前缀开头的密钥：不联网，不打印密钥
import { randomUUID } from "node:crypto";
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, readFileSync, readSync, statSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { basename, join } from "node:path";
import { performance } from "node:perf_hooks";
import { setTimeout as sleep } from "node:timers/promises";
import { parseEnv } from "node:util";
import { arg, flag, sha256 } from "./common.ts";
import { ragKind, ragTask } from "./rag-fake.ts";

/**
 * 认密钥用的前缀（sk- 后面再加几位）：从环境变量 DEEPSEEK_KEY_PREFIX 读，代码和 git 里不写。
 * 本机写在 03_memctx_probe\local.env（不进 git，格式见 local.env.example），real.sh 读进来再传给出口容器。
 */
export function keyPrefix(env: NodeJS.ProcessEnv = process.env): string {
  const prefix = env.DEEPSEEK_KEY_PREFIX?.trim();
  if (!prefix || !/^sk-[A-Za-z0-9_-]{1,16}$/.test(prefix)) {
    throw new Error("没有设 DEEPSEEK_KEY_PREFIX（密钥开头几位，例如 sk-abc）：写进 03_memctx_probe\\local.env，格式见 local.env.example");
  }
  return prefix;
}
export const DEFAULT_UPSTREAM = "https://api.deepseek.com";
/** DeepSeek 官网 deepseek-flash 高峰价，元/百万 token（= 微元/token），2026-11-19 核对。空闲时段是一半；账本一律按高峰价记，是上限。 */
export const PEAK_RATES = { cachedInput: 0.04, uncachedInput: 2, output: 8 } as const;
export type Rates = { cachedInput: number; uncachedInput: number; output: number };
const MAX_BODY_BYTES = 16 * 1024 * 1024;

/**
 * 打码：真实密钥（secrets 里给的）逐字换掉；另外把“像密钥的串”换成 sk-***。像密钥的串只认前面不是字母、数字、下划线、连字符的
 * sk- 加至少 8 位，免得把 task-based、risk-free 这样的正常词改掉。DeepSeek 的正常回复只换真实密钥（见 handle）。
 */
export const redact = (text: string, secrets: readonly string[] = []) => {
  let out = text;
  for (const secret of secrets) if (secret) out = out.split(secret).join("sk-***");
  return out.replace(/(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}/g, "sk-***");
};

/** 文件不在、是空的，或最后一个字节是换行：true。只读最后一个字节（请求体记录可能有几百 MB）。 */
export function endsWithNewline(file: string): boolean {
  if (!existsSync(file)) return true;
  const size = statSync(file).size;
  if (!size) return true;
  const fd = openSync(file, "r");
  try { const b = Buffer.alloc(1); readSync(fd, b, 0, 1, size - 1); return b[0] === 0x0a; } finally { closeSync(fd); }
}

// ---------------------------------------------------------------- 密钥

/**
 * 从密钥文件里找 prefix（默认 DEEPSEEK_KEY_PREFIX）开头的密钥。文件可以是 .env（KEY=VALUE）、JSON，或者纯文本；格式不用事先知道。
 * 地址：只有文件里写了 deepseek.com 的 HTTPS 地址才用它，否则用官方地址；别的主机一律不用，免得把密钥发错地方。
 */
export function readDeepSeekKey(file: string, prefix = keyPrefix()): { apiKey: string; variable: string; baseUrl: string } {
  const text = readFileSync(file, "utf8");
  const found: { name: string; value: string }[] = [];
  try { for (const [name, value] of Object.entries(parseEnv(text))) if (typeof value === "string") found.push({ name, value: value.trim() }); }
  catch { /* 不是 .env */ }
  const walk = (value: unknown, name: string) => {
    if (typeof value === "string") found.push({ name, value: value.trim() });
    else if (value && typeof value === "object") for (const [k, v] of Object.entries(value)) walk(v, k);
  };
  try { walk(JSON.parse(text), "(json)"); } catch { /* 不是 JSON */ }
  for (const m of text.matchAll(/sk-[A-Za-z0-9_-]{8,}/g)) found.push({ name: "(text)", value: m[0] });
  // 只认比前缀长得多的串（前缀本身、只多一两位的不是密钥）
  const key = found.find(f => f.value.startsWith(prefix) && f.value.length >= prefix.length + 8 && /^sk-[A-Za-z0-9_-]+$/.test(f.value));
  if (!key) throw new Error(`密钥文件 ${basename(file)} 里没有 DEEPSEEK_KEY_PREFIX 开头的密钥`); // 不把前缀写进错误文字（会进日志）
  let baseUrl = DEFAULT_UPSTREAM;
  for (const f of found) {
    if (!/base_?url/i.test(f.name)) continue;
    try { const u = new URL(f.value); if (u.protocol === "https:" && /(^|\.)deepseek\.com$/.test(u.hostname)) { baseUrl = f.value.replace(/\/+$/, ""); break; } }
    catch { /* 不是地址 */ }
  }
  return { apiKey: key.value, variable: key.name, baseUrl };
}

// ---------------------------------------------------------------- 账本

type LedgerEvent =
  | { t: "reserve"; id: string; micro: number }
  | { t: "settle"; id: string; micro: number; usage?: unknown }
  | { t: "release"; id: string; reason: string }
  | { t: "retain"; id: string; micro: number; reason: string }
  | { t: "partial-line-skipped" };

/** 金额单位：微元（百万分之一元）。已结清 spent + 保留 held + 在途预扣 reserved 不超过 limit。 */
export class Ledger {
  spent = 0;
  held = 0;
  readonly file: string;
  readonly limit: number;
  private open = new Map<string, number>();
  constructor(file: string, limit: number) {
    this.file = file; this.limit = limit;
    if (existsSync(file)) {
      const lines = readFileSync(file, "utf8").split("\n").filter(l => l.trim());
      let partial = false;
      for (const [n, line] of lines.entries()) {
        let e: LedgerEvent;
        try { e = JSON.parse(line); }
        catch (error) {
          // 只容忍写到一半的一行（进程被强杀）：预扣是先写账本、写完才发请求，写到一半的那笔不会发出去。
          // 它要么是最后一行（这次重启第一次见到），要么后面紧跟着上次重启补的标记。别处的坏行照样报错。
          if (n === lines.length - 1) { partial = true; continue; }
          if (lines[n + 1]?.includes('"partial-line-skipped"')) continue;
          throw error;
        }
        if (e.t === "partial-line-skipped") continue;
        if (e.t === "reserve") this.open.set(e.id, e.micro);
        else if (e.t === "settle") { this.open.delete(e.id); this.spent += e.micro; }
        else if (e.t === "release") this.open.delete(e.id);
        else if (e.t === "retain") { this.open.delete(e.id); this.held += e.micro; }
      }
      // 残片后面补一个换行和一行标记，后面写的记录不会接在残片上，以后每次重启都认得出这行残片。
      if (partial) appendFileSync(file, "\n" + JSON.stringify({ t: "partial-line-skipped", at: new Date().toISOString() }) + "\n");
      // 最后一行完整、只是没写换行（写到右括号就被强杀）：补一个换行，下一笔不会接在它后面
      else if (!endsWithNewline(file)) appendFileSync(file, "\n");
      // 上次进程退出时还在途的预扣：发出去没有，花没花钱都不知道，按全额保留。
      for (const [id] of [...this.open]) this.retain(id, "unfinished before restart");
    }
  }
  get reserved() { let sum = 0; for (const v of this.open.values()) sum += v; return sum; }
  get used() { return this.spent + this.held + this.reserved; }
  private write(event: LedgerEvent) { appendFileSync(this.file, JSON.stringify({ ...event, at: new Date().toISOString() }) + "\n"); }
  reserve(id: string, micro: number): boolean {
    if (this.used + micro > this.limit) return false;
    this.open.set(id, micro); this.write({ t: "reserve", id, micro }); return true;
  }
  settle(id: string, micro: number, usage?: unknown) { this.open.delete(id); this.spent += micro; this.write({ t: "settle", id, micro, usage }); }
  release(id: string, reason: string) { this.open.delete(id); this.write({ t: "release", id, reason }); }
  retain(id: string, reason: string) {
    const micro = this.open.get(id) ?? 0;
    this.open.delete(id); this.held += micro; this.write({ t: "retain", id, micro, reason });
  }
  snapshot() {
    const cny = (micro: number) => Math.round(micro) / 1e6;
    return { limitCny: cny(this.limit), spentCny: cny(this.spent), heldCny: cny(this.held), reservedCny: cny(this.reserved), remainingCny: cny(this.limit - this.used) };
  }
}

/**
 * 按 usage 算费用（微元，向上取整）。DeepSeek 回复里有缓存命中、没命中的 token 数；没有时全按没命中算。
 * 任何一个数缺失、不是非负整数、或命中加没命中不等于输入总数，返回 undefined：按“费用不明”保留预扣（不能让 NaN 进账本，否则上限失效）。
 */
export function costMicro(usage: any, rates: Rates = PEAK_RATES): number | undefined {
  const count = (v: unknown) => (typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : undefined);
  const prompt = count(usage?.prompt_tokens), output = count(usage?.completion_tokens);
  if (prompt === undefined || output === undefined) return undefined;
  const hit = usage.prompt_cache_hit_tokens === undefined ? 0 : count(usage.prompt_cache_hit_tokens);
  const miss = usage.prompt_cache_miss_tokens === undefined ? (hit === undefined ? undefined : prompt - hit) : count(usage.prompt_cache_miss_tokens);
  if (hit === undefined || miss === undefined || miss < 0 || hit + miss !== prompt) return undefined;
  return Math.ceil(hit * rates.cachedInput + miss * rates.uncachedInput + output * rates.output);
}

// ---------------------------------------------------------------- 服务

export interface EgressOptions {
  port: number; host?: string; apiKey: string; upstream: string; model: string; ledgerDir: string; limitMicroCny: number;
  rates?: Rates;
  /** 单次尝试的超时（默认 90 秒）。超时的不重试：DeepSeek 多半还在算、还会收费，再发一次等于付两次。 */
  attemptTimeoutMs?: number;
  /**
   * 一个请求从收到起的总截止（默认 285 秒）：Ditto 用 Node 自带的 fetch 调出口，等回复头最多 300 秒（undici 默认），
   * 新的尝试只在截止前还够 30 秒时才发，每次尝试的超时也不超过截止。
   */
  deadlineMs?: number;
  maxAttempts?: number; backoffMs?: number[]; maxConsecutiveFailures?: number;
}

export interface RequestRow {
  /** 这个请求的编号（格子/题/步骤/序号），responses.jsonl 按它加 “#第几次尝试” 对应。 */
  id: string;
  /** 加在 system 开头的防缓存编号（16 位），由它和 bodies.jsonl 里的原始请求体可以还原发给 DeepSeek 的内容。 */
  rid: string;
  cell: string; task: string; kind: string; sha256: string; bytes: number; sent_bytes: number; at: string;
  attempts: number; upstream_calls: number; status: number; ok: boolean; latency_ms: number | null; total_ms: number;
  usage: any; micro_cny: number | null; finish_reason: string | null; reasoning_chars: number; content: string | null; error: string | null;
  /** 费用不明、保留下来的预扣（微元），计入已花。 */
  held_micro: number;
  /** 每次尝试：发出的时刻、用时、结果、结清或保留的金额、思考字数。 */
  tries: { at: string; ms: number; status: number; outcome: string; micro: number; reasoning_chars: number }[];
}

const MARKER = /^\[task:([^\]]*)\]\[step:(keyword|answer)\]/;

/** 官方 rag-qa 的模型请求（见文件开头第 1 条）：认得出就返回题和步骤，否则 undefined。 */
export function ragKey(messages: readonly { role: string; content: string }[]): { task: string; kind: string } | undefined {
  if (messages.length !== 2 || messages[0]!.role !== "system" || messages[1]!.role !== "user") return undefined;
  const kind = ragKind(messages[0]!.content), task = ragTask(messages[1]!.content);
  return kind && task ? { task, kind } : undefined;
}

export async function startEgress(o: EgressOptions) {
  const rates = o.rates ?? PEAK_RATES;
  const attemptTimeoutMs = o.attemptTimeoutMs ?? 90_000, maxAttempts = o.maxAttempts ?? 3, deadlineMs = o.deadlineMs ?? 285_000;
  const backoff = o.backoffMs ?? [5_000, 15_000], maxConsecutive = o.maxConsecutiveFailures ?? 20;
  const minAttemptMs = Math.min(30_000, attemptTimeoutMs);
  if (!Number.isSafeInteger(o.limitMicroCny) || o.limitMicroCny <= 0) throw new Error("limitMicroCny 必须是正整数（微元）");
  mkdirSync(o.ledgerDir, { recursive: true });
  const ledger = new Ledger(join(o.ledgerDir, "ledger.jsonl"), o.limitMicroCny);
  const requestLog = join(o.ledgerDir, "requests.jsonl");
  const lateLog = join(o.ledgerDir, "late.jsonl");
  const bodiesLog = join(o.ledgerDir, "bodies.jsonl"), bodiesIndex = join(o.ledgerDir, "bodies.index"), responsesLog = join(o.ledgerDir, "responses.jsonl");
  // 记录的最后一行如果写到一半（进程被强杀），先补一个换行，后面的记录不会接在残片上。只读最后一个字节。
  const resetsLog = join(o.ledgerDir, "resets.jsonl");
  for (const file of [requestLog, bodiesLog, bodiesIndex, responsesLog, lateLog, resetsLog]) if (!endsWithNewline(file)) appendFileSync(file, "\n");
  /** 错误文字打码：换掉真实密钥和像密钥的串。 */
  const scrub = (text: string) => redact(text, [o.apiKey]);
  /** DeepSeek 的正常回复：只换真实密钥（万一出现），别的原样记。 */
  const keyOnly = (text: string) => text.split(o.apiKey).join("sk-***");
  // 已经记过的请求体（重启以后接着去重）。摘要在请求体那一行写完以后才写，所以有摘要就一定有请求体；反过来只会多记一次，不会漏。
  // 正在写的（同一个请求体同时来了几次）只写一次；写失败的不算记过，下一次再写。
  const bodiesSeen = new Set(existsSync(bodiesIndex) ? readFileSync(bodiesIndex, "utf8").split("\n").filter(l => /^[0-9a-f]{64}$/.test(l)) : []);
  const bodiesPending = new Set<string>();
  const logBody = (sha: string, bytes: number, body: string) => {
    try {
      appendFileSync(bodiesLog, JSON.stringify({ sha256: sha, bytes, body }) + "\n");
      appendFileSync(bodiesIndex, sha + "\n");
      bodiesSeen.add(sha);
    } catch (error) { console.error(JSON.stringify({ bodyLogFailed: scrub(String((error as Error)?.message ?? error)).slice(0, 200), sha })); }
    finally { bodiesPending.delete(sha); }
  };
  const endpoint = `${o.upstream.replace(/\/+$/, "")}/chat/completions`;
  const state = { cell: "none", nonce: randomUUID(), stopped: false, reason: null as string | null, consecutiveFailures: 0, active: 0, peakActive: 0, errors: 0, upstreamCalls: 0,
    rows: [] as RequestRow[], seq: 0, seen: new Map<string, number>() };
  const stop = (reason: string) => {
    if (state.stopped) return;
    state.stopped = true; state.reason = reason;
    appendFileSync(join(o.ledgerDir, "stopped.jsonl"), JSON.stringify({ at: new Date().toISOString(), reason: scrub(reason), ledger: ledger.snapshot() }) + "\n");
    console.log(JSON.stringify({ stopped: true, reason: scrub(reason) }));
  };
  if (ledger.used >= ledger.limit) stop("budget already used up");

  const send = (res: ServerResponse, status: number, body: unknown) => {
    if (res.headersSent) return;
    res.writeHead(status, { "content-type": "application/json" });
    res.end(typeof body === "string" ? body : JSON.stringify(body));
  };
  const fail = (res: ServerResponse, status: number, code: string, message: string) => send(res, status, { error: { code, message: scrub(message) } });

  async function readBody(req: IncomingMessage): Promise<Buffer> {
    const chunks: Buffer[] = []; let bytes = 0;
    for await (const c of req) { bytes += c.length; if (bytes > MAX_BODY_BYTES) throw new Error("request too large"); chunks.push(Buffer.from(c)); }
    return Buffer.concat(chunks);
  }

  async function handle(req: IncomingMessage, res: ServerResponse) {
    const started = performance.now(), at = new Date().toISOString();
    const raw = await readBody(req);
    let body: any;
    try { body = JSON.parse(raw.toString("utf8")); } catch { state.errors++; return fail(res, 400, "invalid_json", "Request body is not JSON"); }
    if (body?.model !== o.model) { state.errors++; return fail(res, 400, "wrong_model", `Only ${o.model} is allowed`); }
    if (body.stream === true || (Array.isArray(body.tools) && body.tools.length)) { state.errors++; return fail(res, 400, "unsupported", "Streaming and tools are not used in this experiment"); }
    const messages = body.messages;
    if (!Array.isArray(messages) || !messages.length || messages.some((m: any) => typeof m?.content !== "string" || typeof m?.role !== "string")) {
      state.errors++; return fail(res, 400, "invalid_messages", "messages must be text");
    }
    const last = messages.map((m: any) => m.role).lastIndexOf("user");
    const marker = last >= 0 ? MARKER.exec(messages[last].content) : null;
    const rag = marker ? undefined : ragKey(messages);
    if (!marker && !rag) { state.errors++; return fail(res, 400, "no_marker", "Missing internal task marker"); }
    const task = marker ? marker[1]! : rag!.task, step = marker ? marker[2]! : rag!.kind;
    // 同一格里同一题同一步的第几次（见文件开头第 1 条）：第二次起步骤记成 answer#2，记录和比较请求时键不重复。
    const nth = (state.seen.get(`${task}|${step}`) ?? 0) + 1;
    state.seen.set(`${task}|${step}`, nth);
    const kind = nth === 1 ? step : `${step}#${nth}`;
    const out = messages.map((m: any) => ({ role: m.role, content: m.content as string, ...(typeof m.name === "string" ? { name: m.name } : {}) }));
    if (marker) out[last]!.content = out[last]!.content.slice(marker[0].length);
    // 防缓存的编号只放不透明、等长的 16 位哈希：题号里的 “_abs” 是 LongMemEval 的“无法回答”标记，
    // 格子名里有布局名，都不能让模型看见；等长也保证各布局的输入长度一样。可读的格子、题号、步骤记在 requests.jsonl。
    // 哈希里另有一个每格执行一次换一个的随机数：同一格失败后重跑，也不会命中失败那次留下的缓存。
    const rid = sha256(`${state.nonce}|${state.cell}|${task}|${kind}`).slice(0, 16);
    out[0]!.content = `Request ID: ${rid}\n\n${out[0]!.content}`;
    const maxTokens = body.max_tokens ?? body.max_completion_tokens ?? 4096;
    if (!Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > 65_536) { state.errors++; return fail(res, 400, "invalid_max_tokens", "max_tokens out of range"); }
    const sampling = Object.fromEntries(["temperature", "top_p", "stop", "seed"].filter(k => body[k] !== undefined).map(k => [k, body[k]]));
    const payload = JSON.stringify({ model: o.model, messages: out, stream: false, thinking: { type: "disabled" }, max_tokens: maxTokens, ...sampling });
    // 输入 token 数不超过消息的字节数；另给每条消息加 64 个 token 的封装余量（对话模板的特殊 token）；输出按 max_tokens。
    const reserveMicro = Math.ceil((Buffer.byteLength(JSON.stringify(out)) + 64 * out.length) * rates.uncachedInput + maxTokens * rates.output);
    const rawText = raw.toString("utf8"), requestId = `${state.cell}/${task}/${kind}/${state.nonce.slice(0, 8)}.${++state.seq}`;
    const row: RequestRow = { id: requestId, rid, cell: state.cell, task, kind, sha256: sha256(rawText), bytes: raw.length, sent_bytes: Buffer.byteLength(payload), at,
      attempts: 0, upstream_calls: 0, status: 0, ok: false, latency_ms: null, total_ms: 0, usage: null, micro_cny: null, held_micro: 0, finish_reason: null,
      reasoning_chars: 0, content: null, error: null, tries: [] };
    // 原始请求体全文，同一个只记一次（见文件开头）。回复 Ditto 以后才写（响应结束的 close 事件）：第一次见到的请求体才要写，
    // 写在回复之前的话，先跑的格子每个请求都多等一下，对各布局不对称。
    if (!bodiesSeen.has(row.sha256) && !bodiesPending.has(row.sha256)) {
      bodiesPending.add(row.sha256);
      res.once("close", () => logBody(row.sha256, raw.length, rawText));
    }
    let reply: { status: number; body: string } | undefined;
    const deadline = started + deadlineMs;
    /** 退避以后，截止前还够 minAttemptMs 才再试；不够就不试了。 */
    const retryAfter = async (attempt: number) => {
      if (attempt >= maxAttempts) return false;
      const wait = backoff[attempt - 1] ?? backoff.at(-1) ?? 1000;
      if (performance.now() + wait + minAttemptMs > deadline) return false;
      await sleep(wait); return true;
    };
    try {
      for (let attempt = 1; attempt <= maxAttempts && !reply; attempt++) {
        row.attempts = attempt;
        if (state.stopped) { row.status = 503; row.error = `egress stopped: ${state.reason}`; break; }
        const id = `${requestId}#${attempt}`;
        if (!ledger.reserve(id, reserveMicro)) { stop(`budget: ${ledger.snapshot().remainingCny} CNY left, request needs up to ${reserveMicro / 1e6}`); row.status = 503; row.error = "budget exhausted"; break; }
        const t0 = performance.now(), tryAt = new Date().toISOString();
        // 这次尝试的用时：收到 DeepSeek 的回复为止，不含之后本地写记录、记账的时间；没收到回复时是到出错为止
        let upstreamMs: number | undefined;
        const tried = (status: number, outcome: string, micro: number, reasoning = 0) =>
          row.tries.push({ at: tryAt, ms: upstreamMs ?? performance.now() - t0, status, outcome, micro, reasoning_chars: reasoning });
        const hold = (reason: string) => { ledger.retain(id, reason); row.held_micro += reserveMicro; };
        let status = 0, text = "";
        // 这一次尝试最多等多久（整数毫秒）。到点就不再等：同时中止请求，但不指望中止一定生效。
        // 11-19 那一轮有两次 DeepSeek 先给了响应头、约 10 分钟后才给回答，AbortSignal.timeout(90 秒) 没有打断读回答，
        // 本机（同一个 Node 24.14，HTTP、HTTPS、强制 GC）复现不出来，原因没查清。所以改成自己的计时器和请求赛跑，
        // 保证出口在截止前一定答复编排器（docs\20 第 7 节）。
        const limitMs = Math.max(1, Math.floor(Math.min(attemptTimeoutMs, deadline - performance.now())));
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout> | undefined, gaveUp = false;
        const call = (async () => {
          const r = await fetch(endpoint, { method: "POST", redirect: "error", signal: controller.signal,
            headers: { authorization: `Bearer ${o.apiKey}`, "content-type": "application/json" }, body: payload });
          return { status: r.status, text: await r.text() };
        })();
        // 放弃以后请求要是自己又完成了，记进 late.jsonl 备查（预扣已经按费用不明保留，账本不动）。
        call.then(late => {
          if (!gaveUp) return;
          // 这里抛出的错误没人接，会让出口进程退出：写不进就只在出口日志里留一行，账本不受影响。
          const ms = performance.now() - t0;
          try {
            let usage: unknown = null; try { usage = JSON.parse(late.text)?.usage ?? null; } catch { /* 读不出就算了 */ }
            appendFileSync(lateLog, JSON.stringify({ cell: row.cell, task, kind, attempt: id, at: tryAt, ms, status: late.status, usage,
              micro: costMicro(usage, rates) ?? null }) + "\n");
          } catch (error) { console.error(JSON.stringify({ lateLogFailed: scrub(String((error as Error)?.message ?? error)).slice(0, 200), attempt: id })); }
          try {
            const ok = late.status >= 200 && late.status < 300;
            appendFileSync(responsesLog, JSON.stringify({ id, at: tryAt, ms, status: late.status, late: true, body: ok ? keyOnly(late.text) : scrub(late.text) }) + "\n");
          } catch (error) { console.error(JSON.stringify({ responseLogFailed: scrub(String((error as Error)?.message ?? error)).slice(0, 200), attempt: id })); }
        }, () => { /* 被中止或断线：不用管 */ });
        try {
          state.upstreamCalls++; row.upstream_calls++;
          const timeout = new Promise<never>((_, reject) => {
            timer = setTimeout(() => {
              gaveUp = true;
              const reason = new DOMException(`attempt exceeded ${limitMs} ms`, "TimeoutError");
              controller.abort(reason); reject(reason);
            }, limitMs);
          });
          ({ status, text } = await Promise.race([call, timeout]));
          upstreamMs = performance.now() - t0;
        } catch (error) {
          // 发出去以后断线或超时：DeepSeek 可能已经算了钱，保留预扣（按费用不明记）。
          const message = scrub(String((error as Error)?.message ?? error)).slice(0, 300);
          hold(`network: ${message.slice(0, 200)}`);
          row.status = 0; row.error = message;
          const timedOut = (error as Error)?.name === "TimeoutError";
          tried(0, timedOut ? "timeout-held" : "network-held", reserveMicro);
          // 超时也重试（截止前还够一次的话）。11-19 的两次超时都是 DeepSeek 排队；再发一次最多多付一次回答的钱（约 0.05 元），
          // 被放弃的那次已经按全额预扣保留，100 元上限照样卡得住；不重试的话这题一定作废（docs\21）。
          if (await retryAfter(attempt)) continue;
          break;
        } finally { clearTimeout(timer); }
        // DeepSeek 这次尝试的完整回复（见文件开头）。写不进只在日志里留一行：不能让记录出错被当成断线、再付一次钱。
        try { appendFileSync(responsesLog, JSON.stringify({ id, at: tryAt, ms: upstreamMs, status, body: status >= 200 && status < 300 ? keyOnly(text) : scrub(text) }) + "\n"); }
        catch (error) { console.error(JSON.stringify({ responseLogFailed: scrub(String((error as Error)?.message ?? error)).slice(0, 200), attempt: id })); }
        row.status = status;
        if (status >= 200 && status < 300) {
          let parsed: any;
          try { parsed = JSON.parse(text); } catch { parsed = undefined; }
          const choice = parsed?.choices?.[0];
          if (!choice || typeof choice.message?.content !== "string") {
            hold("unreadable 2xx response"); row.error = "unreadable response"; tried(status, "unreadable-held", reserveMicro);
            if (await retryAfter(attempt)) continue;
            break;
          }
          const reasoning = typeof choice.message.reasoning_content === "string" ? choice.message.reasoning_content.length : 0;
          row.reasoning_chars += reasoning;
          const micro = costMicro(parsed.usage, rates);
          if (micro === undefined) { hold("response without valid usage"); row.error = "response without valid usage"; tried(status, "usage-unknown-held", reserveMicro, reasoning); }
          else {
            row.micro_cny = (row.micro_cny ?? 0) + micro; ledger.settle(id, micro, parsed.usage); tried(status, "settled", micro, reasoning);
            // 实际费用超过了预扣：说明预扣的假设（输入 token ≤ 字节数）不成立，上限可能被突破。照实记账，立刻停。
            if (micro > reserveMicro) stop(`settled cost ${micro} micro-CNY exceeded the reservation ${reserveMicro}; reservation rule needs review`);
          }
          row.usage = parsed.usage ?? null; row.latency_ms = upstreamMs ?? null;
          row.finish_reason = choice.finish_reason ?? null; row.content = String(choice.message.content).slice(0, 8000);
          // DeepSeek 推理资源不足时 finish_reason 是 insufficient_system_resource，生成被打断；Ditto 不认这个值，会当失败。
          // 钱照算（上面已结清），当临时故障重试。
          if (choice.finish_reason === "insufficient_system_resource") {
            row.error = "insufficient_system_resource";
            if (await retryAfter(attempt)) continue;
            break;
          }
          row.ok = true; row.error = micro === undefined ? row.error : null;
          reply = { status: 200, body: text };
          break;
        }
        // 有状态码的错误回复：DeepSeek 不收钱，放掉预扣。
        ledger.release(id, `http ${status}`);
        row.error = scrub(text).slice(0, 300);
        tried(status, "http-error-released", 0);
        if (status === 401 || status === 403) { stop(`upstream refused the key (HTTP ${status})`); break; }
        if (status === 402) { stop("upstream balance insufficient (HTTP 402)"); break; }
        if ((status === 429 || status >= 500) && await retryAfter(attempt)) continue;
        break; // 400、422 等：重试也不会好
      }
    } finally {
      row.total_ms = performance.now() - started;
      state.rows.push(row);
      // 写不进只在日志里留一行，照样回复：已经付费的回答不能因为记录出错变成失败、让这一题作废
      try { appendFileSync(requestLog, JSON.stringify(row) + "\n"); }
      catch (error) { console.error(JSON.stringify({ requestLogFailed: scrub(String((error as Error)?.message ?? error)).slice(0, 200), id: row.id })); }
    }
    if (reply) { state.consecutiveFailures = 0; return send(res, 200, reply.body); }
    state.errors++;
    if (!state.stopped && ++state.consecutiveFailures >= maxConsecutive) stop(`${state.consecutiveFailures} consecutive failed requests`);
    // 503 只表示出口自己停了（含预算用完）；上游的失败一律 502，两者不混。
    return fail(res, state.stopped ? 503 : 502, state.stopped ? "egress_stopped" : "upstream_failed",
      `${row.error ?? "upstream failed"} (HTTP ${row.status})`);
  }

  const settings = () => ({ cell: state.cell, model: o.model, upstreamHost: new URL(endpoint).host, rates, attemptTimeoutMs, maxAttempts, deadlineMs, backoffMs: backoff });
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://x");
    void (async () => {
      if (req.method === "GET" && url.pathname === "/stats") {
        return send(res, 200, { settings: settings(), rows: state.rows, peakActive: state.peakActive, errors: state.errors,
          realModelCalls: state.upstreamCalls, ledger: ledger.snapshot(), stopped: state.stopped, reason: state.reason });
      }
      if (req.method === "GET" && url.pathname === "/status") {
        return send(res, 200, { stopped: state.stopped, reason: state.reason, active: state.active, ledger: ledger.snapshot() });
      }
      if (req.method === "POST" && url.pathname === "/reset") {
        await readBody(req);
        if (state.stopped) return fail(res, 503, "egress_stopped", `egress stopped: ${state.reason}`);
        const cell = url.searchParams.get("seed");
        if (!cell || !/^[A-Za-z0-9_.-]+$/.test(cell)) return fail(res, 400, "invalid_cell", "seed must name the cell");
        Object.assign(state, { cell, nonce: randomUUID(), rows: [], peakActive: 0, errors: 0, upstreamCalls: 0, seen: new Map() });
        appendFileSync(resetsLog, JSON.stringify({ at: new Date().toISOString(), cell, nonce: state.nonce }) + "\n");
        return send(res, 200, settings());
      }
      if (req.method === "POST" && url.pathname.endsWith("/chat/completions")) {
        state.active++; state.peakActive = Math.max(state.peakActive, state.active);
        try { return await handle(req, res); } finally { state.active--; }
      }
      res.writeHead(404); res.end();
    })().catch(error => fail(res, 500, "egress_error", String((error as Error)?.message ?? error)));
  });
  server.keepAliveTimeout = 600_000;
  server.headersTimeout = 610_000;
  server.requestTimeout = 0;
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(o.port, o.host ?? "0.0.0.0", resolve); });
  const port = (server.address() as { port: number }).port;
  return {
    url: `http://127.0.0.1:${port}`, port, state, ledger,
    close: () => new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }),
  };
}

// ---------------------------------------------------------------- 命令行

if (process.argv[1]?.replaceAll("\\", "/").endsWith("/egress.ts")) {
  if (flag("check-key")) {
    const file = arg("check-key");
    try {
      const k = readDeepSeekKey(file);
      // 只说找到了、在哪个变量里，不打印密钥和前缀
      console.log(JSON.stringify({ found: true, file: basename(file), variable: k.variable,
        // 只打印协议、主机和路径（地址里万一夹着用户名、密码或查询参数，不进日志）。
        upstream: `${new URL(k.baseUrl).origin}${new URL(k.baseUrl).pathname.replace(/\/+$/, "")}/chat/completions` }));
    } catch (error) { console.log(JSON.stringify({ found: false, file: basename(file), reason: redact(String((error as Error).message)) })); process.exitCode = 1; }
  } else {
    const key = readDeepSeekKey(arg("key-file"));
    const egress = await startEgress({
      port: Number(arg("port", "8080")), apiKey: key.apiKey, upstream: arg("upstream", key.baseUrl), model: arg("model", "deepseek-flash"),
      ledgerDir: arg("ledger"), limitMicroCny: Math.round(Number(arg("limit-cny")) * 1e6),
    });
    console.log(JSON.stringify({ ready: true, port: egress.port, model: arg("model", "deepseek-flash"), upstreamHost: new URL(arg("upstream", key.baseUrl)).host,
      keyVariable: key.variable, ledger: egress.ledger.snapshot(), stopped: egress.state.stopped }));
    const shutdown = async () => { await egress.close(); process.exit(0); };
    process.once("SIGTERM", () => void shutdown());
    process.once("SIGINT", () => void shutdown());
  }
}
