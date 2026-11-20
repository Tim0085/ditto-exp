/**
 * 官方 HTTP 传输的请求体、响应体上限（createHttpTransport / createWorkerHttpHandler 的 maxBodyBytes，默认 1 MiB）。
 * 拆开 CONTEXT 后，LOAD、UPDATE 返回整个工作集（LongBench 中段一题最大约 2 MB），超过默认值，所以放到 16 MiB。
 * 这是官方提供的参数（docs/worker-communication.zh-CN.md“默认 body 上限 1 MiB”），两端设成一样。
 */
export const MAX_BODY_BYTES = 16 * 1024 * 1024;
