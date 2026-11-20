"""把阶段一到三的表合成一个 Excel，列名换成中文。只是换格式，不做计算。

    python -I src/workbook.py <输出 xlsx> <名字=csv 路径> ...
"""
import csv, sys
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

NAMES = {
    "config": "配置", "scenario": "场景", "tasks": "题数", "task_wall_ms": "每题墙钟(ms)", "orchestrator_ms": "编排器本身(ms)",
    "other_workers_ms": "留在编排器进程的其他Worker(ms)", "MEMORY_ms": "MEMORY(ms)", "CONTEXT_ms": "CONTEXT(ms)",
    "MEMORY_calls": "MEMORY调用次数", "CONTEXT_calls": "CONTEXT调用次数", "MEMORY_transfer_kb": "MEMORY进出数据(KB)",
    "CONTEXT_transfer_kb": "CONTEXT进出数据(KB)", "MEMORY_caller_serde_ms": "MEMORY调用方序列化(ms)",
    "MEMORY_worker_serde_ms": "MEMORY被拆方序列化(ms)", "CONTEXT_caller_serde_ms": "CONTEXT调用方序列化(ms)",
    "CONTEXT_worker_serde_ms": "CONTEXT被拆方序列化(ms)", "target_share": "目标Worker占比",
    "layout": "布局", "cores": "核数", "http_ms": "每次HTTP固定开销(ms)", "concurrency": "并发",
    "mono_demand_ms": "Monolithic每题需求(ms)", "split_demands_ms": "拆开后各进程需求(ms)", "mono_tps": "Monolithic吞吐(题/s)",
    "split_tps": "拆开吞吐(题/s)", "throughput_gain": "吞吐提高", "mono_latency_ms": "Monolithic平均用时(ms)",
    "split_latency_ms": "拆开平均用时(ms)", "latency_cut": "平均用时缩短", "upper_bound_gain": "吞吐上限提高",
    "cell": "格子", "group": "组", "round": "轮", "failed": "失败题数", "first_error": "第一个错误", "steady_completed": "稳态完成题数",
    "throughput_per_s": "吞吐(题/s)", "latency_mean_ms": "平均用时(ms)", "latency_p50_ms": "p50(ms)", "latency_p95_ms": "p95(ms)",
    "latency_p99_ms": "p99(ms)", "window_s": "稳态窗口(s)", "fake_peak_active": "假模型最多同时请求", "fake_errors": "假模型错误",
    "real_model_calls": "真实模型调用", "total_cores_used": "合计用掉的核", "requests_same_as_mono": "请求体与Monolithic相同",
    "orchestrator_main_busy_share": "编排器主线程忙比例", "orchestrator_cores_used": "编排器容器用核",
    "orchestrator_loop_delay_p99_ms": "编排器事件循环延迟p99(ms)", "orchestrator_mem_peak_mb": "编排器容器内存峰值(MB)",
    "memory_main_busy_share": "MEMORY主线程忙比例", "memory_cores_used": "MEMORY容器用核", "memory_loop_delay_p99_ms": "MEMORY事件循环延迟p99(ms)",
    "memory_mem_peak_mb": "MEMORY容器内存峰值(MB)", "context_main_busy_share": "CONTEXT主线程忙比例", "context_cores_used": "CONTEXT容器用核",
    "context_loop_delay_p99_ms": "CONTEXT事件循环延迟p99(ms)", "context_mem_peak_mb": "CONTEXT容器内存峰值(MB)",
    "mono_throughput": "Monolithic吞吐(题/s)", "split_throughput": "拆开吞吐(题/s)", "mono_p95_ms": "Monolithic p95(ms)",
    "split_p95_ms": "拆开p95(ms)", "p95_cut": "p95缩短", "mono_mean_ms": "Monolithic平均(ms)", "split_mean_ms": "拆开平均(ms)",
    "requests_same": "请求体相同", "failed_mono": "Monolithic失败", "failed_split": "拆开失败",
    # 全量实验（docs\\31）：Direct 和合并对照
    "requests_same_as_direct": "请求体与Direct相同", "direct_throughput": "Direct吞吐(题/s)", "layout_throughput": "这一布局吞吐(题/s)",
    "direct_p95_ms": "Direct p95(ms)", "layout_p95_ms": "这一布局p95(ms)", "direct_mean_ms": "Direct平均(ms)", "layout_mean_ms": "这一布局平均(ms)",
    "failed_direct": "Direct失败", "failed_layout": "这一布局失败", "failed_model_output": "模型输出不合格题数",
    "memctx_main_busy_share": "MEMORY+CONTEXT进程主线程忙比例", "memctx_cores_used": "MEMORY+CONTEXT进程用核",
    "memctx_loop_delay_p99_ms": "MEMORY+CONTEXT进程事件循环延迟p99(ms)", "memctx_mem_peak_mb": "MEMORY+CONTEXT进程内存峰值(MB)",
    "interaction_main_busy_share": "INTERACTION进程主线程忙比例", "interaction_cores_used": "INTERACTION进程用核",
    "interaction_loop_delay_p99_ms": "INTERACTION进程事件循环延迟p99(ms)", "interaction_mem_peak_mb": "INTERACTION进程内存峰值(MB)",
}


# 这些列一律按文字写：题号（有的题号全是数字、有前导零）、回答、错误等，转成数字会丢前导零或变成科学计数
TEXT = {"题号", "id", "回答", "标准答案", "运行错误", "检索词", "判分失败", "结束原因", "first_error", "cell", "格子", "group", "组", "round", "轮",
        "layout", "布局", "requests_same", "requests_same_as_mono", "requests_same_as_direct", "比较", "A", "B", "题型"}


def number(v):
    try:
        return int(v) if v.lstrip("-").isdigit() else float(v)
    except (ValueError, AttributeError):
        return v


wb = Workbook()
wb.remove(wb.active)
for spec in sys.argv[2:]:
    title, path = spec.split("=", 1)
    sheet = wb.create_sheet(title)
    with open(path, encoding="utf-8-sig") as f:
        header = []
        for i, row in enumerate(csv.reader(f)):
            if i == 0:
                header = row
                sheet.append([NAMES.get(v, v) for v in row])
                continue
            sheet.append([v if (header[k] if k < len(header) else "") in TEXT else number(v) for k, v in enumerate(row)])
            # 以等号开头的文字（模型的回答里可能有）按文字写，不当公式
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.data_type = "s"
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "B2"
    for i in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(i)].width = 15
wb.save(sys.argv[1])
print("写出", sys.argv[1])
