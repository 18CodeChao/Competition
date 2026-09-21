# 未来战争：参赛程序与本地判题器（平台反馈修复版 v3）

目标 Python **3.11.10**，仅依赖标准库。官方 docs 和 Demo 保留原样。
可直接使用本轮生成的 [dist/submission-v3.zip](dist/submission-v3.zip)，解压后根目录包含 `run.sh`。
本版使用指定后方三炮位、固定工人守炮、严格区分两方机器人，以及平台标准输出日志。
本版策略与验证边界见 [docs/PLATFORM_V3.md](docs/PLATFORM_V3.md)；
历史 v2 策略与模拟假设见 [docs/EXPERIENCE_V2.md](docs/EXPERIENCE_V2.md)；
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
python -m local_judge --seeds 17 --opponent v2 --swap --rounds 1040 --report reports/my-development.json
python -m local_judge --seeds 607 --opponent v2 --swap --rounds 1040 --report reports/my-holdout.json
python -m local_judge --seeds 1 --opponent v2 --unknown-waves growth --swap --report reports/my-stress.json
```

`--swap`为同种子左右各运行一次；每场最多1300轮，报告同时保存半场与双半场本地结果。
默认 `--profile observed` 使用用户反馈的基地和前八夜波次；第九、十夜未知，
`--unknown-waves hold8`（默认）沿用第八夜，`growth` 是人为递增压力情景，均非官方数据。
`--opponent v2`（默认）加载 `reports/baseline-v2.zip`；`previous` 加载 v1，双方使用同一环境。
1040轮只覆盖已知的前8天，不是完整比赛，不计算最终胜负。
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
真实Python运行版本和本版实测结果见 [reports/VALIDATION_V3.md](reports/VALIDATION_V3.md)。
第一版及开发中间报告保留作历史证据，不代表最终源码验证。

## 对局日志与地图

正式程序不创建本地日志文件，完整请求、响应、动作反馈、布局、选敌/忽略列表、任务状态、
升级/撤退事件、敌情、价格和每轮41×32地图全部输出到 stdout，由平台统一采集。
长记录分段编号，每行以 `COMPETITION_LOG ` 开始，及时 flush。`--map-every 5` 可降低地图频率。
框架异常输出到 stderr。移除了 `--log-dir` 参数；不要把旧启动参数带入 v3。

```powershell
python -m local_judge.analyze 平台下载日志.txt --round 71 --side challenger --output reports/log-analysis.txt
```

分析器支持平台时间戳前缀、分段重组和旧JSONL，统计缺段及损坏记录。请保留平台原始文本日志，
不要只截屏。预测伤害不等于已确认击杀；任务答案和工具结果也记录在日志中。

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
