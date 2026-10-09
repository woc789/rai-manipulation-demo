"""Read-only incident presentation; never infer physical success or retry motion."""

from dataclasses import dataclass

from .order import readable_error


@dataclass(frozen=True)
class Incident:
    title: str
    reason: str
    impact: str
    next_step: str
    scan: bool = False
    cancel_waiting: bool = False
    recover: bool = False


def describe_incident(state, reason, synced, job=None, can_recover=False):
    job = job or {}
    job_state = job.get("state")
    cancelable = job_state in ("QUEUED", "WAITING_STOCK", "WAITING_BIN") and not job.get("completed", 0)
    if not synced:
        return Incident("通信 · 状态未同步", "未收到最新工作台状态，不能确认操作结果。",
                        "历史订单和箱位不是当前现场保证。", "等待通信恢复；不要重复下单或运动。")
    if state == "INVENTORY_ERROR":
        display_reason = "视觉计算等待超时，暂时无法确认库存。" if "timed out" in reason.lower() or "timeout" in reason.lower() else readable_error(reason)
        return Incident("库存感知 · 复查失败", display_reason,
                        "订单已保留，自动配货暂停；不等于订单未接收或确认缺货。",
                        "先检查相机数据，再重试库存复查；详细步骤见处理指南。",
                        scan=True, cancel_waiting=cancelable)
    if state == "RECOVERY_REQUIRED" or job_state == "EXECUTION_UNCERTAIN":
        return Incident("运动 / 现场 · 需要人工核对", readable_error(reason or job.get("reason", "")),
                        "商品可能已移动或仍在夹爪中，不能盲目重新抓取。",
                        "先核对夹爪和订单箱；恢复会清理现场，待取货箱需先确认取货。",
                        recover=can_recover)
    if state == "SCANNING":
        return Incident("库存感知 · 正在复查", "正在读取相机并检测、定位桌面商品。",
                        "订单保留并等待结果，尚未开始机械臂配货。", "请等待；无需重复提交或重复复查。")
    if job_state == "WAITING_STOCK":
        return Incident("订单调度 · 等待库存核对", readable_error(job.get("reason", "")),
                        "该订单尚未执行，可用观测不足不等于已经确认实物缺货。",
                        "重试库存复查；确认实际缺货后再使用补货准备。", scan=True, cancel_waiting=cancelable)
    if job_state == "WAITING_BIN":
        return Incident("订单调度 · 等待空闲箱位", job.get("reason", ""),
                        "已配好的订单箱继续保留，不会被新订单覆盖。",
                        "在订单箱位中确认已完成订单取货，或取消这份等待订单。", cancel_waiting=cancelable)
    if job_state == "FAILED":
        return Incident("订单执行 · 本单失败", readable_error(job.get("reason", "")),
                        f"已完成 {job.get('completed', 0)}/{job.get('total', 0)}；失败不是成功装箱。",
                        "查看日志确认原因。运动状态未明时先人工核对；不要直接重复抓取。")
    return None


def resolution_steps(state, job=None):
    job = job or {}
    if state == "INVENTORY_ERROR" or job.get("state") == "WAITING_STOCK":
        return [
            "点击“检查相机数据”，约 2 秒后查看图像、深度、相机参数是否收到。",
            "缺少消息：查看仿真是否退出或暂停，恢复运行后再检查。先不要重复下单，也不要用补货掩盖相机故障。",
            "三路都有消息：点击“重试库存复查”。原订单会保留；复查成功后调度器重新检查资源，不必再提交同一订单。",
            "复查成功但仍等待库存：查看视觉库存与桌面实物。确认实际缺货后使用“补货准备”；若只是漏检，先复查，不能直接认定缺货。",
            "相机正常但复查仍超时：查看技术日志，保留失败时间和订单号，进一步定位检测、分割或点云阶段；不连续重试、不直接恢复整个桌面。",
            "不想继续等待：点击“取消此等待订单”，确认后只取消这份尚未执行的订单。",
        ]
    if state == "RECOVERY_REQUIRED" or job.get("state") == "EXECUTION_UNCERTAIN":
        return ["查看仿真：夹爪是否夹着商品、商品是否已入箱，不能依据超时推断没有移动。",
                "先确认待取货订单取货；暂停或部分装箱订单需核对并按现有流程处理。",
                "只有“恢复工作台”按钮可用时才操作，并阅读清理确认。按钮不可用说明安全条件未满足，不要绕过。",
                "恢复会清理现场，不是无损重试；确认场景和库存重新就绪后再下单。"]
    if job.get("state") == "WAITING_BIN":
        return ["查看下方订单箱位，找到“待取货”的订单。",
                "确认已经取走该订单，再选择订单号点击“确认取货”；仿真会移除对应箱子和商品。",
                "箱位释放后，等待订单重新参与调度；不要重复下单。"]
    if job.get("state") == "FAILED":
        return ["查看本单原因和技术日志，区分识别失败、运动失败与装箱校验失败。",
                "如果工作台要求人工核对，按现场核对流程处理，不能直接重发抓取。",
                "若工作台已就绪且确认没有未处理商品，调整订单或排除识别问题后，用新订单号提交；失败单本身不会自动重跑。"]
    return ["等待最新工作台状态；通信未恢复时无法确认操作结果，不要重复发送订单。"]
