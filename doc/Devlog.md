# Devlog

Development timeline — newest first. Record milestone results and verified conclusions, not process noise.

---

## 2026-02-27 — 规划：实时跟踪模式（手部/人脸）

**决策：** 未来要做一个独立的实时跟踪交互模式，与现有 VLM 驱动的程序化运动并行存在，可在运行时切换。

**动机：** 当前系统是 VLM → 抽象参数 → 程序化运动，适合"感知-表演"场景。但直接的手部/人脸跟踪是另一种交互范式——1:1 位置映射，观众操控感更强。两者应共存。

**核心思路：**
- 本地检测器（MediaPipe 等）提取手/脸坐标，线性映射到机械臂笛卡尔空间
- 复用现有 servo pipeline（velocity clamp + bounds clamp）
- 超出可达范围时，用 RPY 倾斜表达"够不到"的身体语言（lean）
- 接近边界时 soft margin 减速，避免突然停顿
- 作为新的 motion mode 接入 `ParametricMotionGenerator`，不替换现有模式

**实施路线：** 分步验证——先跑通 MediaPipe 检测，再做坐标映射，再接入 motion mode，最后加 lean 和 soft margin。

详见 [Design Brief — Future: Real-Time Tracking Mode](Design%20Brief.md)

---

## 2026-02-27 — Servo Mode 压力测试 + 修复 3 个可靠性 Bug

**背景：** Servo Mode (Mode 1) 是刚实现的实时流式控制，替代 Mode 0 的 FIFO 队列。压力测试发现了 3 个 bug，全部修复后 8 个测试阶段全部通过。

**测试脚本：** `test_servo_stress.py`（8 个阶段，`--quick` 约 80 秒完成）

| 阶段 | 测试内容 | 结果 |
|------|---------|------|
| 1. 持续 25Hz 流式 | CALM 模式连续发送 | PASS (24.7 Hz, 0 errors) |
| 2. 快速模式切换 | 6 个模式每 2 秒轮换 | PASS (7 次切换, 0 errors) |
| 3. 极端位置 | 安全边界 8 个角 | PASS (4 个角 IK 不可达但无错误) |
| 4. 速度极端 | DORMANT↔EXCITED 突变 | PASS (修复后) |
| 5. Pitch 摆动 | PLAYFUL 模式 J5 快速点头 | PASS (修复后) |
| 6. 随机轰炸 | 每 0.1 秒随机参数+随机模式 | PASS |
| 7. 边界浸泡 | 故意超出边界，验证 clamp | PASS (全部在界内) |
| 8. 恢复测试 | 强制触发 error 60，验证自动恢复 | PASS |

**发现并修复的 3 个 Bug：**

1. **`enable_servo()` 不验证 `set_mode(1)` 是否成功** — 错误恢复后 `set_mode(1)` 返回 code=10（未就绪），但代码不检查。后续所有 servo 命令在 Mode 0 下运行，产生大量 "mode may be incorrect" 警告。
   - **修复：** 加入 3 次重试 + 完整重置循环 + 异步模式确认等待（最多 1 秒）+ 返回 True/False

2. **`_recover()` 直接调 `set_mode(1)` 而非 `enable_servo()`** — 恢复后 servo 模式恢复失败时无感知、无重试。
   - **修复：** 改用 `enable_servo()` 统一入口，打印恢复结果

3. **EXCITED 模式工作空间过大，触发 Joint Angle Limit (error 23)** — 从 DORMANT 极近位置突跳到 EXCITED 全幅，经过不可达的关节角组合。
   - **修复：** 振幅从 0.45/0.45/0.35 降到 0.35/0.30/0.28，谐波叠加从 30/25mm 降到 20/18mm。视觉效果仍然足够戏剧化

**附加：** ERROR_NAMES 增加 code 60（Servo Cartesian Out of Range）

**Phase 3 观察 — IK 不可达角不是 bug：**
- 安全边界定义的是一个长方体，但臂的实际工作空间是球形/环形
- X=100（极近基座）+ Y=±200（极侧方）或 Z=850（极高）组合超出关节活动范围
- SDK 不报错，只是到不了目标位置（drift 最大 442mm）
- 不影响正常使用（运动生成器不会产生这种极端组合）

---

## 2026-02-26 — Layer 1: AI Perception Pipeline (Gemini 2.5 Flash) 跑通

**验证结果：**
- 完整 pipeline：Webcam (5fps) → 5帧批量 → Gemini 2.5 Flash (structured JSON) → 6个连续参数 → 参数化运动生成 → 机械臂
- Gemini 返回结构化 JSON（Pydantic schema 强制），6个 float 全部在有效范围内
- 单次 VLM 调用延迟 ~5 秒，1Hz 更新频率下约每 6 秒更新一次感知状态
- 指数移动平均（EMA）平滑避免参数跳变

**架构决策：**
- **不用 Gemini Live API** — 标准 `generateContent` API 更简单、更便宜（$0.30/M tokens）、原生 JSON schema、无需 WebSocket 管理
- **多帧传入**（5帧/次）让 VLM 能看到时序变化，识别动态手势（摇头、招手等），单帧只能识别静态姿势
- **Persona YAML** 定义交互人格 — system prompt + motion params + safety bounds，换文件 = 换性格，零代码改动
- **6个连续参数**（energy/attention_x/attention_y/mood/presence/urgency）驱动所有运动变化，替代之前的 4 个离散状态
- **Provider 抽象** — `VLMProvider` ABC，当前用 Gemini，可随时换 Claude/GPT

**踩坑：**
- Windows 上 `cv2.VideoCapture()` 在后台线程会永久阻塞 — 必须在主线程打开摄像头，然后传给后台线程
- `print()` 在 Windows 多线程中需要 `flush=True` 否则输出延迟

**新文件：**
- `scripts/arm_controller.py` — 从 vlm_motion_poc.py 提取的可复用 ArmController
- `scripts/perception.py` — VLM provider 抽象 + Gemini 实现 + 摄像头线程 + 感知线程
- `scripts/motion_gen.py` — 参数化运动生成器（连续参数 → 正弦波 XYZ）
- `scripts/persona.py` — PerceptionState 数据类 + StateHolder + YAML 加载器
- `scripts/main.py` — 完整 pipeline 入口
- `personas/default.yaml` — "Curious Creature" 默认人格

**运行方式：**
- `python scripts/main.py --ip 127.0.0.1` — 完整 pipeline（摄像头 + VLM + 机械臂）
- `python scripts/main.py --keyboard` — 键盘手动控制参数
- `python scripts/main.py --no-camera` — 模拟摄像头 + VLM
- `python scripts/test_perception_static.py` — 静态图/摄像头快照测试 VLM
- `python scripts/test_perception_live.py` — 连续感知循环测试（无机械臂）

---

## 2026-02-26 — Houdini ↔ 机械臂坐标映射验证通过

**验证结果：**
- Houdini 场景（Y-up 右手系，单位：米）→ 机械臂坐标（Z-up，单位：mm）映射完全正确
- 三轴方向标定测试：Hou +X/+Y/+Z 分别对应臂前伸/上抬/左移，与 Houdini 视口一致
- 缩放比 ×1000（Houdini 1 单位 = 1 米 = 1000mm），臂模型高度 0.8 Hou = 800mm 实测匹配

**确定的映射关系（`houdini_to_arm()`）：**

| Houdini | → Arm | 含义 |
|---------|-------|------|
| +X | +X | 前（臂面朝方向） |
| +Y | +Z | 上 |
| +Z | +Y | 左（右手系） |

**关键决策：去掉 auto-fit，使用直通变换**
- 用户在 Houdini 里导入了臂模型作参考，相对模型摆放曲线
- 之前的 `auto_fit_to_workspace` 会任意缩放+平移，破坏空间关系
- 改为 `houdini_to_arm()`：只做轴交换 + ×1000，不缩放不平移
- What you see in Houdini is what the arm does

**踩坑：**
- `auto_fit_to_workspace` 的 bug：常量轴（range=0）给默认 scale=1.0，`min(scales)` 取到它，导致整条路径没放大（总长才 2.8mm）
- 标定缩放时不能从任意中心点缩放，必须从臂底座原点 (0,0,0) 缩放才能保持方向正确

**脚本：**
- `scripts/loop_path.py` — 加载 Houdini 路径并循环播放
- `scripts/loop_axis_test.py` — 三轴方向标定循环测试
- `scripts/test_axis_directions.py` — 单次三轴方向测试
- `scripts/axis_calibrate.py` — 交互式六方向标定

---

## 2026-02-25 — 连续运动 + 自动错误恢复机制验证

**验证结果：**
- 3D Lissajous figure-8 轨迹在模拟器上连续运行，无需人工干预
- SDK 回调机制 (`register_error_warn_changed_callback`) 可实时捕获错误
- 自动恢复 4 步序列可行：`clean_error()` → `motion_enable(True)` → `set_mode(0)` → `set_state(0)`

**关键发现：错误处理不需要点 Web UI 弹窗**
- 所有错误（碰撞、超速、超限等）都可以通过 SDK 的 `clean_error` + 重新使能来自动恢复
- Web UI 弹窗只是前端行为，SDK 层面完全可以绕过
- 错误码 31（碰撞异常电流）是最常见的碰撞检测码

**踩坑：**
- `set_collision_sensitivity()` 在 Docker 模拟器上会永久阻塞 — 模拟器固件不支持该命令，需要超时跳过
- `arm.state` 初始化就是 4（停止态），每次连接后都需要执行完整的使能序列

**脚本：** `scripts/loop_motion.py` — 可用 `--sensitivity` 参数控制碰撞灵敏度（真机用）

---

## 2026-02-25 — MVP: Python SDK → Docker Simulator 通路验证通过

**验证结果：**
- 完整通路跑通：`Python script (.venv) → xArm-Python-SDK → TCP 127.0.0.1:30000 → Docker container (850 firmware) → UFactory Studio Web UI`
- 测试脚本 `test_sim_connection.py`：连接、启用、移动到两个位置、读回位置确认，全部成功
- SDK 版本 xarm-python-sdk 1.17.3，固件 v2.4.0

**踩坑记录：**
- Git Bash 在 Windows 上会把 `/bin/bash` 重写为 Windows 路径，Docker 命令里要用双斜杠 `//bin/bash`
- Docker 容器用 `-d` 启动会立即退出，需要在 entrypoint 末尾加 `sleep infinity` 保持运行
- `.venv` 要先加 `.gitignore` 再创建，避免误提交

**结论：** Vibe coding / agent 写代码操控 Docker 模拟器这条路可行，可以继续在此基础上迭代。

---

## 2026-02-25 — Project Setup

- 建立项目 repo，写 Design Brief（三层架构方向），配好文档结构
- 三层架构是工作方向但明确标记为 tentative

**Open questions:**
- VLM 选型（Claude Vision / GPT-4V / other）
- 是否用本地模型做前置过滤
- TouchDesigner 集成时机
