"""ModelSpec v2：P2 支持 5 种模型类型（场景族边界见 docs/non-goals.md）。

- single_queue_multi_server : 单队列多服务台（M/M/c，无限队列）
- finite_queue              : 有限队列 + 流失（M/M/c/K，等位区满则离开）
- multi_stage_line          : 多阶段服务链（前台→服务→收银，100% 顺流）
- peak_arrival              : 分时段到达（高峰/平峰，非齐次泊松·薄化法）
- priority_queue            : 优先级队列（VIP 优先，非抢占）

字段按 model_type 条件必填，由 model_validator 检查；
校验失败信息会回灌 LLM（防线①），因此错误信息必须具体、可执行。
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ArrivalProcess(BaseModel):
    """到达过程。「平均每 X 分钟来一位」→ mean_interval = X。"""

    distribution: Literal["exponential"] = "exponential"
    mean_interval: float = Field(gt=0, description="平均到达间隔（分钟）")


class ServiceProcess(BaseModel):
    """服务过程。"""

    distribution: Literal["exponential"] = "exponential"
    mean_service_time: float = Field(gt=0, description="平均服务时长（分钟）")


class PeakPeriod(BaseModel):
    """peak_arrival 的一个客流时段。所有时段必须首尾相接、从 0 开始并覆盖仿真时长。"""

    start_min: float = Field(ge=0, description="时段开始（分钟，从仿真 0 时刻起算）")
    end_min: float = Field(gt=0, description="时段结束（分钟），须大于 start_min")
    mean_interval: float = Field(gt=0, description="该时段内平均到达间隔（分钟）")


class ServiceStage(BaseModel):
    """multi_stage_line 的一个服务阶段。"""

    name: str = Field(min_length=1, max_length=30, description="阶段名（如：登记/检查/出报告）")
    num_servers: int = Field(ge=1, le=200, description="该阶段并行服务台数")
    mean_service_time: float = Field(gt=0, description="该阶段平均服务时长（分钟）")


class SimulationConfig(BaseModel):
    """仿真运行配置。"""

    duration: float = Field(gt=0, description="仿真时长（分钟），如 8 小时 = 480")
    random_seed: int = Field(default=42, ge=0, le=2**31 - 1)
    replications: int = Field(default=1, ge=1, le=5, description="评测集固定 1 次")


class ModelSpec(BaseModel):
    """仿真模型 spec（v2，按 model_type 条件校验）。"""

    model_type: Literal[
        "single_queue_multi_server",
        "finite_queue",
        "multi_stage_line",
        "peak_arrival",
        "priority_queue",
    ]
    scenario_name: str = Field(min_length=2, max_length=60, description="简短场景名（中文）")

    # —— 条件字段 ——
    arrival_process: Optional[ArrivalProcess] = None
    service_process: Optional[ServiceProcess] = None
    num_servers: Optional[int] = Field(default=None, ge=1, le=200)
    queue_capacity: Optional[int] = Field(default=None, ge=1, le=999, description="仅 finite_queue 必填；None = 无限")
    vip_share: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="仅 priority_queue 必填；VIP 占比 0~1")
    peak_periods: Optional[List[PeakPeriod]] = Field(default=None, description="仅 peak_arrival 必填，2~4 个时段")
    stages: Optional[List[ServiceStage]] = Field(default=None, description="仅 multi_stage_line 必填，2~4 个阶段")

    queue_rule: Literal["fifo", "priority"] = "fifo"
    simulation: SimulationConfig

    @model_validator(mode="after")
    def check_fields_for_model_type(self) -> "ModelSpec":
        mt = self.model_type
        if mt in ("single_queue_multi_server", "finite_queue", "priority_queue"):
            if self.arrival_process is None:
                raise ValueError(f"{mt} 缺少 arrival_process（到达过程）")
            if self.service_process is None:
                raise ValueError(f"{mt} 缺少 service_process（服务过程）")
            if self.num_servers is None:
                raise ValueError(f"{mt} 缺少 num_servers（并行服务台数量）")
        if mt == "finite_queue" and self.queue_capacity is None:
            raise ValueError("finite_queue 必须给出 queue_capacity（等位区/排队容量上限）")
        if mt == "priority_queue":
            if self.vip_share is None:
                raise ValueError("priority_queue 必须给出 vip_share（优先客户占比，0~1 小数）")
            self.queue_rule = "priority"
        if mt == "multi_stage_line":
            if self.arrival_process is None:
                raise ValueError("multi_stage_line 缺少 arrival_process（到达过程）")
            if not self.stages or len(self.stages) < 2 or len(self.stages) > 4:
                raise ValueError("multi_stage_line 必须给出 stages（2~4 个阶段，按流程顺序）")
        if mt == "peak_arrival":
            if self.service_process is None:
                raise ValueError("peak_arrival 缺少 service_process（服务过程）")
            if self.num_servers is None:
                raise ValueError("peak_arrival 缺少 num_servers（并行服务台数量）")
            if not self.peak_periods or len(self.peak_periods) < 2 or len(self.peak_periods) > 4:
                raise ValueError("peak_arrival 必须给出 peak_periods（2~4 个时段）")
            periods = sorted(self.peak_periods, key=lambda p: p.start_min)
            if periods[0].start_min != 0:
                raise ValueError("peak_periods 的第一个时段必须从 0 开始")
            for a, b in zip(periods, periods[1:]):
                if b.start_min != a.end_min:
                    raise ValueError(
                        f"peak_periods 时段必须首尾相接：时段 [{a.start_min},{a.end_min}] 与 "
                        f"[{b.start_min},{b.end_min}] 之间有缝隙或重叠"
                    )
            if periods[-1].end_min < self.simulation.duration:
                raise ValueError(
                    f"peak_periods 必须覆盖整个仿真时长：最后时段结束于 {periods[-1].end_min}，"
                    f"但仿真时长为 {self.simulation.duration}"
                )
            self.peak_periods = periods
        return self
