# 未来战争：参赛程序与本地判题器（新闻推理与安全卡位版 v8）

目标 Python **3.11.10**，仅依赖标准库。官方 docs 和 Demo 保留原样。
可直接使用本轮生成的 [dist/submission-v8.zip](dist/submission-v8.zip)，解压后根目录包含 `run.sh`。
本版使用指定后方三炮位、固定工人守炮、严格区分两方机器人，以及平台标准输出日志。
本轮新闻分渠道推理、第一天侦察、背面待命、卡位顺序和双工人操炮见 [docs/NEWS_RECON_V8.md](docs/NEWS_RECON_V8.md)；
此前围墙抢修、个人备料和备用炮位见 [docs/DEFENSE_RAIDS_V7.md](docs/DEFENSE_RAIDS_V7.md)；
A/B 失败原因、修复与回合优化见 [docs/TASK_WORKFLOWS_V6.md](docs/TASK_WORKFLOWS_V6.md)；
中文日志和任务安全见 [docs/LOG_TASK_V5.md](docs/LOG_TASK_V5.md)；
此前 v4 任务修复见 [docs/LOG_TASK_V4.md](docs/LOG_TASK_V4.md)；
此前平台修复见 [docs/PLATFORM_V3.md](docs/PLATFORM_V3.md)；
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
`--opponent v7`（默认）加载 `reports/baseline-v7.zip`；`v6`、`v5`、`v4`、`v3`、`v2`和`previous`保留旧对手，双方使用同一环境。
observed环境按用户最新反馈改为机器人以基地为目标，仅攻击挡路单位；具体路径选择仍是本地假设。
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
真实Python运行版本和本版实测结果见 [reports/VALIDATION_V8.md](reports/VALIDATION_V8.md)。
第一版及开发中间报告保留作历史证据，不代表最终源码验证。

## 对局日志与地图

正式程序只输出stdout，由平台采集。默认使用中文 `Round / Request / Response` 分回合格式。
记录金币、分数、基地血量、每个角色独立的位置/血量/背包、本轮指令与上一轮反馈。
非空的任务原文、答案、判题反馈、新闻、LLM返回、沙盒结果、Prompt和executeCmd均记录原文。
session和代码版本只在启动记录一次；不重复完整请求、哈希、敌情历史或整张地图。
本轮采集只是下发指令；下一轮动作合法且背包增加后才记录“确认获得”。答案错因只来自实际判题反馈。
地图默认关闭，`--map-every 130` 可启用。框架异常输出到 stderr，不使用 `--log-dir`。

中文日志直接人工阅读；旧结构化分析器仍可配合显式 `python main.py 8080 --log-format json` 使用：

```powershell
python -m local_judge.analyze 平台下载日志.txt --round 71 --side challenger --output reports/log-analysis.txt
```

分析器支持 JSON 模式的 `BATTLE` / `BATTLE_PART`、平台时间戳前缀、分段重组、旧JSONL；不解析默认中文文本。
完整错误说明原样记录；判题器未提供错项时明确写未知，不将动作合法判为答案正确。
简明日志不包含所有原始观测，无法完整重放旧版全部决策；优先满足人工复盘。
样例见 [reports/v5-log-example.txt](reports/v5-log-example.txt)。
v6 沙盒结果直接提交的样例见 [reports/v6-answer-log-example.txt](reports/v6-answer-log-example.txt)。
v7 抢修、预测受伤与卡位动作的样例见 [reports/v7-log-example.txt](reports/v7-log-example.txt)。
v8 跨日传闻、宝藏提交及双工人操炮见 [reports/v8-log-example.txt](reports/v8-log-example.txt)。

## 文件职责

- `agent/rules.py`：官方常量、坐标、建造区。
- `agent/protocol.py`、`audit.py`：报文结构与执行条件分层审计、资源预留。
- `agent/world.py`：只依据公开观测构图和寻路。
- `agent/tactics.py`：维修工个人库存、日间缺口重建、回防、侦察与卡位调度。
- `agent/recon.py`：第一天侦察、敌方武器记忆、背面待命、单/双缺口和炮位卡位。
- `agent/news.py`：官方新闻与民间传闻分开积累、明确线索本地提取、模型证据校验。
- `agent/combat.py`：弹道伤害与多炮目标选择。
- `agent/tasks.py`：新闻记忆、任务SOP和跨回合LLM协议。
- `agent/sandbox_tasks.py`：传送到官方沙盒执行的规范修复、接口适配、全量分页及统计；参赛进程不执行其中的文件修复/HTTP查询。
- `agent/policy.py`、`strategy.py`：经济、防守、任务、宝藏与角色调度。
- `agent/layout.py`：三炮共享位置、迎敌墙和后方通路检查。
- `agent/intelligence.py`：已见敌情、历史受伤、价格和进攻评估。
- `agent/telemetry.py`：结构化日志与ASCII地图。
- `agent/server.py`、`main.py`、`run.sh`：比赛入口。
- `local_judge/`：模拟环境、换边、回放与实验报告。
- `tests/`：规则和接口回归。

参赛提交至少包含 `agent/`、`main.py`、`run.sh`。不需要将模拟器与报告加载进参赛程序。
