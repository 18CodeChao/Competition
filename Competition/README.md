# 未来战争：参赛程序与本地判题器（经验优化版 v2）

目标 Python **3.11.10**，仅依赖标准库。官方 docs 和 Demo 保留原样。
本版采用迎敌半圈墙、共享操炮位的三火箭、实时溅射选靶、夜间分工和升级回血。
本版策略与假设见 [docs/EXPERIENCE_V2.md](docs/EXPERIENCE_V2.md)；
基础接口、图片解读及未覆盖规则见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。

## 启动参赛程序

Linux／正式评测入口，在本目录运行：

```bash
bash run.sh 8080
```

Windows 本地也可以：

```powershell
python main.py 8080
```

监听 `0.0.0.0:8080`，接收根路径 HTTP POST JSON，返回 `roleCommandMap`、`prompt`、`executeCmd`。
默认系统解释器必须是3.11或更新版本；`run.sh`支持用`PYTHON`指定解释器。
没有额外依赖、密钥、外网模型调用或安装步骤。不要修改官方 request.txt/response.txt 来制造通过。

## 本地模拟

```powershell
python -m local_judge --seeds 1 --opponent previous --swap --report reports/my-development.json
python -m local_judge --seeds 303 404 --opponent previous --swap --report reports/my-holdout.json
python -m local_judge --seeds 1 --opponent previous --unknown-waves growth --swap --report reports/my-stress.json
```

`--swap`为同种子左右各运行一次；每场最多1300轮，报告同时保存半场与双半场本地结果。
默认 `--profile observed` 使用用户反馈的基地和前八夜波次；第九、十夜未知，
`--unknown-waves hold8`（默认）沿用第八夜，`growth` 是人为递增压力情景，均非官方数据。
`--opponent previous` 加载本次修改前保存的 `reports/baseline-v1.zip`，双方使用同一环境。
`--profile legacy` 保留第一版假设环境。`--pressure`也是本地压力参数。
题库、LLM/沙盒服务替身、机器人AI均有覆盖限制，本地胜负不是官方胜率。

记录逐轮回放（可生成较大的JSONL文件）：

```powershell
python -m local_judge --seeds 101 --rounds 130 --replay reports/replay.jsonl --report reports/replay-summary.json
```

连接已经启动的HTTP参赛程序：

```powershell
python -m local_judge --url http://127.0.0.1:8080/ --opponent demo --seeds 101 --swap --report reports/http.json
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试包含独立预期的规则案例、HTTP往返、官方请求样例、错误输入，以及注入LLM/命令结果的跨轮任务闭环。
真实Python运行版本和本版实测结果见 [reports/VALIDATION_V2.md](reports/VALIDATION_V2.md)。
第一版及开发中间报告保留作历史证据，不代表最终源码验证。

## 对局日志与地图

正常启动自动写入 `logs/`：完整请求、响应、动作反馈、布局、目标伤害预测、升级/撤退事件、
对手已见建筑、价格历史，以及每轮41×32 ASCII地图。日志按64MiB滚动，每类保留8份备份。
通过 `python main.py 8080 --log-dir logs --map-every 5` 可改目录与地图间隔。

```powershell
python -m local_judge.analyze logs/实际文件名.jsonl --round 71 --side challenger --output reports/log-analysis.txt
```

可读样例：[第71轮地图与HTTP日志统计](reports/v2-http-analysis.txt)。后续请提供 JSONL 和判题器原始日志；
发生滚动时一并提供对应 `.jsonl.1` 等文件。预测伤害不等于已确认击杀。

## 文件职责

- `agent/rules.py`：官方常量、坐标、建造区。
- `agent/protocol.py`、`audit.py`：报文结构与执行条件分层审计、资源预留。
- `agent/world.py`：只依据公开观测构图和寻路。
- `agent/combat.py`：弹道伤害与多炮目标选择。
- `agent/tasks.py`：新闻记忆、任务SOP和跨回合LLM协议。
- `agent/policy.py`、`strategy.py`：经济、防守、任务、宝藏与角色调度。
- `agent/layout.py`：三炮共享位置、迎敌墙和后方通路检查。
- `agent/intelligence.py`：已见敌情、历史受伤、价格和进攻评估。
- `agent/telemetry.py`：结构化日志与ASCII地图。
- `agent/server.py`、`main.py`、`run.sh`：比赛入口。
- `local_judge/`：模拟环境、换边、回放与实验报告。
- `tests/`：规则和接口回归。

参赛提交至少包含 `agent/`、`main.py`、`run.sh`。不需要将模拟器与报告加载进参赛程序。
