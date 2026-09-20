# 未来战争：第一版参赛程序与本地判题器

目标 Python **3.11.10**，仅依赖标准库。官方 docs 和 Demo 保留原样。
规则、变更分类、图片解读、假设与未覆盖项见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。

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
python -m local_judge --seeds 1 7 --opponent demo --swap --report reports/development.json
python -m local_judge --seeds 101 202 --opponent self --swap --report reports/holdout.json
python -m local_judge --seeds 101 --opponent demo --pressure 2 --swap --report reports/stress.json
```

`--swap`为同种子左右各运行一次；每场最多1300轮，报告同时保存半场与双半场本地结果。
`--pressure`是未获官方规定的本地压力参数，不改变武器/血量/价格等官方规则。
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
真实Python运行版本和实测结果见 [reports/VALIDATION.md](reports/VALIDATION.md)。

## 文件职责

- `agent/rules.py`：官方常量、坐标、建造区。
- `agent/protocol.py`、`audit.py`：报文结构与执行条件分层审计、资源预留。
- `agent/world.py`：只依据公开观测构图和寻路。
- `agent/combat.py`：弹道伤害与多炮目标选择。
- `agent/tasks.py`：新闻记忆、任务SOP和跨回合LLM协议。
- `agent/policy.py`：经济、防守、任务、宝藏调度。
- `agent/server.py`、`main.py`、`run.sh`：比赛入口。
- `local_judge/`：模拟环境、换边、回放与实验报告。
- `tests/`：规则和接口回归。

参赛提交至少包含 `agent/`、`main.py`、`run.sh`。不需要将模拟器与报告加载进参赛程序。
