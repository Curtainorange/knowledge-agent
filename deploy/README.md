# 部署到工作室服务器（Windows Server · 80 端口子路径）

目标形态：

```
浏览器 ──► Nginx :80 ──/cognition/──► uvicorn 127.0.0.1:8010 ──► E:\projects\renzhifujia
                                                                    .env / dev.db / data\ / models\
```

- 对外只有 **80** 一个口，靠子路径 `/cognition/` 与其他项目区分；
- 应用只绑 **127.0.0.1**，不经 Nginx 进不来；
- 应用由 **Windows 服务**托管：谁登录、谁注销都不影响它。

本目录的三个文件：`install_service.bat`（安装服务）、`update.bat`（每周更新）、
`nginx/cognition.conf`（Nginx 片段，部署到项目自己的 `nginx\` 目录，照 RoamSync 的结构）。

---

## 零、对共享环境的写入面（先看这张表）

这台服务器上还有别的项目，本方案的原则：**自己的目录随便写，共享的东西只读 + 追加，
每一步都能回滚**。

| 共享资源 | 本方案的动作 | 风险控制 |
|---|---|---|
| `E:\projects\renzhifujia` | 新建，本项目的独占目录 | 无 |
| `E:\projects\端口占用与入口链接.txt` | **追加**一行 | 不覆盖、不改别人写的行 |
| `D:\Software` | 仅在服务器缺 Python 3.12 时新装一个独立目录；NSSM 已有就**复用** | 不动已有版本，**不改系统 PATH** |
| Nginx 配置 | 在自己项目目录新增 `nginx\cognition.conf`（照 RoamSync 的 `roamsync.conf`）；主配置**最多加一行 include**，通配则零改动 | 不往 nginx 的 conf 目录添文件、不改他人 location、改主配置前先备份 |
| Windows 服务 / 计划任务 | 新建 `cognition-copilot` 一项 | 名字唯一，安装前查重名 |
| 防火墙 | **默认不改** | 别的项目能从 80 访问，就说明规则已存在 |
| 网络端口 | 只用 `127.0.0.1:8010` | 部署前探测占用，冲突就换 |
| 系统 Python | 不安装到 PATH、不改 `py` launcher | 全程用绝对路径调用 `.venv` 里的解释器 |

**本方案明确不做的事**：不装全局 pip 包、不改系统 PATH、不动 `nginx.conf` 里的全局段与
他人 `location`、不重启 IIS 或其他服务、不碰其他项目的目录与数据库。

唯一的「全局动作」是 `nginx -s reload`——它会重载所有站点的配置，耗时毫秒级。
配置校验（`nginx -t`）不通过时 reload 不会被接受，master 继续用旧配置服务，
所以**即使我们的片段写错，也不会把别人的站点弄挂**。

---

## 0. 先探测，别装重复的东西

在服务器上开 **管理员 cmd**，逐条跑，结果记下来：

```cmd
where python & python -V
where nginx & nginx -v
netstat -ano | findstr ":8010 :80"
dir E:\projects
type "E:\projects\端口占用与入口链接.txt"
```

三条**必查**的冲突项：

```cmd
rem ① 子路径是否已被别人占用（大小写不敏感；有输出就换前缀，比如 /copilot）
findstr /i /n "cognition" D:\Software\nginx\conf\nginx.conf

rem ② 是否已有同名服务/计划任务（有输出说明名字被占，改 SVC 名）
sc query cognition-copilot
schtasks /query /tn "cognition-copilot"

rem ③ 8010 是否被别人占（有输出就看 PID：tasklist /fi "pid eq <PID>"）
netstat -ano | findstr ":8010"
```

**④ 确认「正在跑的是哪个 nginx」**——这条不能省。服务器上若装过不止一份 nginx，
`where nginx` 拿到的不一定是正在监听 80 的那份，改错文件等于白改：

```cmd
netstat -ano | findstr ":80"
rem 在输出里找 0.0.0.0:80 或 [::]:80 那一行，最后一列是 PID
powershell -Command "Get-Process -Id <上面的PID> | Select-Object Path"
```

拿到的 `Path` 就是正在运行的 nginx.exe，它的 `conf\nginx.conf` 才是要改的那个。

**⑤ 把登记表整份看完**（`type "E:\projects\端口占用与入口链接.txt"`）。
已知在跑的项目：本草灵诊（8080/5173/11434）、ShopAgent（18080/8081/18090）、
RoamSync（80 共用 Nginx，内部 3002/8002）。表里可能还有别的，看完整份再动手。

判断规则：

| 情况 | 处理 |
|---|---|
| Python **≥ 3.12 已存在** | **直接复用**，不要再装一份（服务器约定：已有环境不重复安装）。记下它的绝对路径 |
| Python 低于 **3.12** | 新装 3.12 到 `D:\Software\Python312`，**不动已有的**（见下方安装注意） |
| 8010 已被占用 | 换一个空端口，同步改 `.env`/脚本里的 `PORT`，并登记 |
| `/cognition` 已被占用 | 换前缀，同时改 `.env` 的 `ROOT_PATH` 与 Nginx 片段 |
| Nginx 已存在且在跑 | 用它，**只加自己的 conf 文件**（见第 6 步），不重启 nginx 主进程 |
| E 盘权限 | 确保 `stu1`/`stu2` 对 `E:\projects\renzhifujia` 有「修改」权限 |

**装 Python 3.12 时的三个勾，别勾错**（勾错会改掉别人脚本依赖的环境）：

- ❌ 不勾 **Add python.exe to PATH**——勾了会把 3.12 插到 PATH 前面，
  别人的 `python` 命令从此指向新版本；
- ❌ 不勾 **py launcher**——它会更新全局的 `py` 启动器，可能改变别人 `py -3.x` 的行为；
- ✅ 勾 **Install for all users**，路径填 `D:\Software\Python312`。

装完用绝对路径调用即可，本方案其余步骤全部是 `D:\Software\Python312\python.exe`
或 `.venv\Scripts\python.exe`，不依赖 PATH。

## 1. 目录与独立环境

用第 0 步**探测到的那个 ≥3.12 的解释器**建 venv。服务器上已有三个项目在跑，
很可能早就装过 3.12，**优先复用，别再装一份**——下面的路径只是示例，按实际替换：

```cmd
mkdir E:\projects\renzhifujia
D:\Software\Python312\python.exe -m venv E:\projects\renzhifujia\.venv
```

只借那个解释器**建 venv**，不要往它的 site-packages 里装任何东西。
venv 建成后，所有依赖都装进 `E:\projects\renzhifujia\.venv`，
与已有三个项目的环境完全隔离。

venv 必须放在项目内，别放 `C:\Users\stu1\`——两个账号的用户目录不共享。

## 2. 上传代码

代码和模型分开打包——`models/` 在 `.gitignore` 里，不在提交包里，但**没有它语义检索会降级**。

```bash
# ① 代码：必须在**含最新提交的工作区**里打
#    本项目同时开着多个 worktree（D:\知识学习智能体 在 loop/book-digest 分支，
#    不含子路径改造的提交），在旧目录里打包会打出一个没有本次改动的包。
cd /c/Users/Curtain/WorkBuddy/Worktrees/知识学习智能体/main-3a131297
git log --oneline -1                       # 先确认是 b3b82fd 之后的提交
git archive --format=zip -o /d/deploy-code.zip HEAD

# ② 模型：数据文件不属于任何分支，从主目录拷
cd /d/知识学习智能体
tar -czf /d/deploy-models.tar.gz models    # 约 24MB
```

传到服务器三种方式，按条件选：

1. **RDP 剪贴板**：直接在远程桌面里 `Ctrl+V` 粘贴文件（小文件最省事）；
2. **RDP 驱动器映射**：远程桌面 → 显示选项 → 本地资源 → 详细信息 → 勾本地盘，
   登录后服务器上会多出 `\\tsclient\D`，直接从中复制；
3. **共享文件夹 / 网盘**：文件大、网络稳时用。

服务器上解压（先代码、后模型，都解到项目根）：

```cmd
cd /d E:\projects\renzhifujia
powershell -Command "Expand-Archive -Path '%USERPROFILE%\Desktop\deploy-code.zip' -DestinationPath '.' -Force"
tar -xzf "%USERPROFILE%\Desktop\deploy-models.tar.gz" -C .
```

> zip 用 PowerShell 解，别用 `tar -xf`：PATH 里若是 git 附带的 GNU tar，它不认 zip，
> 会报 `not a tar archive`。`tar.gz` 两种 tar 都能解。

`.env`、`dev.db`、`data\`、`models\` 都不在版本库里，**更新时不要覆盖它们**。
有 git 且能访问远端时，直接 `git clone` 更省事，模型仍要单独拷。
**已经手工拷过一次的目录**，不用重来——按 **9.0** 补成 git 仓库即可，拷进去的代码会被仓库版本替换。

## 3. 依赖与向量模型

```cmd
cd /d E:\projects\renzhifujia
.venv\Scripts\python.exe -m pip config --site set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pip install onnxruntime tokenizers
```

> 第一行把镜像源写进**这个 venv**（`--site`），只影响本项目；不带 `--site` 会写到用户级
> `%APPDATA%\pip\pip.ini`，把 stu1/stu2 名下所有项目一起改掉。

> 第三行不是冗余：`pyproject.toml` 里没有声明这两个包（本机 anaconda 自带了，
> 所以本地永远绿）。缺了它，`EMBEDDING_BACKEND=local` 会**静默降级**成关键词检索。

模型二选一：

```cmd
rem A. 从本机拷：把 models\bge-small-zh-v1.5 整个目录复制到服务器同名路径
rem B. 服务器能访问 modelscope.cn 时：
.venv\Scripts\python.exe scripts\fetch_embedding_model.py
```

拷贝后核一下目录里至少有 `config.json`、`tokenizer.json`、`onnx\model_quantized.onnx`
与同名 `.onnx_data`（两个文件缺一不可）。

## 4. 配置 `.env`

```cmd
copy .env.example .env
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

把输出填进 `JWT_SECRET=`，并确认：

```ini
ROOT_PATH=                    # **必须留空**，理由见下方红字
DATABASE_URL=sqlite:///./dev.db
EMBEDDING_BACKEND=local
EMBEDDING_LOCAL_DIR=models/bge-small-zh-v1.5
WORKER_ENABLED=true
```

> **`ROOT_PATH` 必须留空**（10-27 已在真机踩过）。`FastAPI(root_path=...)` 在当前
> FastAPI 版本里是废弃参数，填了值会让 `app.mount("/assets")` 匹配不上 ——
> 表现是**页面能开、CSS/JS 全部 404**（返回 `{"detail":"Not Found"}`），
> 第一眼极像 Nginx 配错了。前缀在 Nginx 那层已经剥掉（§6.6 第 2 点），
> 应用自己不需要知道 `/cognition` 的存在。

`JWT_SECRET` 留空的后果：进程每次重启随机生成，两个账号会不停被踢下线。

数据库不用初始化——`app/main.py` 启动时 `create_all` 自动建表并补列。
**不要另外跑 `alembic upgrade head`**，会和 `create_all` 打架。

## 4.5 先在前台跑通，再装服务

这一步不能跳。装成服务后报错只会进日志，前台跑能当场看见第一条异常：

```cmd
cd /d E:\projects\renzhifujia
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

看到 `Application startup complete.` 即成功。**别关这个窗口**，另开一个 cmd 验：

```cmd
curl.exe -s http://127.0.0.1:8010/health
```

期望返回 `{"status":"ok",...}` 之类的 JSON。确认无误后回到前台窗口按 `Ctrl+C` 停掉，
再往下装服务（两者抢同一个端口）。

启动即失败时按这个顺序看：

| 报错 | 原因 |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | 工作目录不对，必须先 `cd /d E:\projects\renzhifujia` |
| `ModuleNotFoundError: fastapi` / `onnxruntime` | 依赖没装到 `.venv`，检查是不是用了系统 python |
| `Address already in use` | 8010 被占，`netstat -ano \| findstr :8010` 查 PID |
| 卡在加载模型几十秒 | 正常，首次加载 ONNX 需要时间；之后走缓存 |

## 5. 装成常驻服务（本机用任务计划，NSSM 为备选）

**这台服务器上实际用的是任务计划**（`cognition-copilot`，SYSTEM / ONSTART），
`nssm.exe` 没装——所以下面的 schtasks 一条命令就是全部安装动作，优先用它。
NSSM 方案保留在后面，换机器时可用（多崩溃重启与日志轮转，日常维护更顺手）。

```cmd
schtasks /create /tn "cognition-copilot" /ru SYSTEM /sc ONSTART /delay 0000:20 ^
  /tr "cmd /c cd /d E:\projects\renzhifujia && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 >> logs\app.log 2>&1" /f
schtasks /run /tn "cognition-copilot"
```

启停：`schtasks /run /tn "cognition-copilot"` / `schtasks /end /tn "cognition-copilot"`。
`/sc ONSTART` 与是否登录无关，重启机器后自动起来。

> 10-27 建任务时误用了 `/sc ONCE`（一次性、无自启），服务器 11-04 重启后服务没起来，
> 对外表现为 `502`。**自启的成本为零，不该省。**

**启动参数里不要加 `--workers`**：SQLite + 进程内向量索引 + 后台 worker 线程，
多进程会重复跑任务、抢写库。默认单进程就是对的。

### 5.1 看门狗（补上「崩溃不会自动重启」这个缺口）

任务计划是**开机跑一次**的语义：进程一旦崩掉就再也不起来，`/sc ONSTART` 只保证下次开机。
10-27 那次用了 `/sc ONCE`，11-04 重启后服务没起、对外 502；11-09 又是一次 502
（nginx 活着、8010 没人接）。NSSM 那种服务壳带崩溃重启，但这台机器没装 NSSM。

补法是一个外部探活，不需要装任何东西：

```cmd
cd /d E:\projects\renzhifujia
deploy\watchdog.bat install     rem 建 cognition-watchdog，每 5 分钟一次，SYSTEM
deploy\watchdog.bat status      rem 看任务状态 + 上游健康 + 最近 10 行日志
deploy\watchdog.bat remove      rem 只删这一个任务，不碰 cognition-copilot
```

它做什么：每 5 分钟探一次 `http://127.0.0.1:8010/health`，**连续 3 次不是 200** 才动手；
动手时先 `schtasks /end` 再 `/run`，然后最多等 40 秒复检；健康时一行日志都不写。
日志 `logs\watchdog.log` 只在异常时增长，所以出现 `STILL DOWN` 就是真需要人看。

三个必须知道的细节（都是踩过的）：

1. **它会和 `update.bat` 抢**。停服到起服之间有几十秒窗口，正好被探到就是「用旧代码把服务拉起来」，
   最坏结果是更新脚本探到 200、报成功，而实际跑的是旧进程。所以 `update.bat` 现在会自动
   `/disable` 它、结束时再 `/enable`（失败路径也会恢复）。**手工更新时记得自己关掉它。**
2. **探活必须带 `--noproxy`**。设了 `HTTP_PROXY` 的机器上，代理会把 `127.0.0.1:8010` 接管并返回 502；
   实测：8010 上没人监听，`curl` 却拿到 502。不绕过代理，watchdog 会认为服务永远挂着、每 5 分钟重启一次。
3. **`deploy\*.bat` 只能写 ASCII**。cmd 按 OEM 代码页（中文 Windows 是 GBK）解析批处理，
   UTF-8 的中文注释会被解成乱码字节、吞掉行尾，注释片段被当命令执行。中文说明一律写在本文件里。
   门禁：`tests/test_deploy_scripts.py` 会检查每个 `.bat` 没有非 ASCII 字节。

它的边界：机器关机、磁盘满、端口被别人占，watchdog 都救不了（只会记一行 `STILL DOWN`）。

<details>
<summary>备选：用 NSSM 装成 Windows 服务</summary>

`nssm.exe` 是单文件绿色工具（nssm.cc，选 win64 版）。**先看服务器上有没有，有就复用**：

```cmd
dir D:\Software\nssm\nssm.exe
where nssm
```

已有则直接用（只调用、不覆盖，符合服务器「已装环境不重复装」的约定）；没有才下载，
放 `D:\Software\nssm\`，或放自己的 `E:\projects\renzhifujia\tools\nssm.exe` 完全隔离。
脚本里的 `NSSM` 变量按实际路径改。

安装前先确认服务名没被占（第 0 步已查过，重名就改脚本里的 `SVC`）：

```cmd
cd /d E:\projects\renzhifujia
deploy\install_service.bat
```

脚本做四件事：设工作目录（`.env`/`dev.db`/`data`/`models` 全是相对路径，错了就是空库）、
日志重定向到 `logs\app.log` 并 10MB 轮转、开机自启、崩溃自动重启。
它只创建/操作 `cognition-copilot` 这一个服务，不会停、改、删任何其他服务。
`nssm.exe` 不存在时脚本会直接 abort，不会误建第二个实例。

</details>

## 6. 挂到 80 端口的子路径（照 RoamSync 的结构）

服务器上已有同类先例：**RoamSync** 挂在 `/roamsync/`、对外共用 80 端口，
配置放在自己项目目录 `E:\projects\RoamSync-runtime\nginx\roamsync.conf`。
本项目照抄这个结构——配置放自己目录、由统一 Nginx include，
**不往 nginx 的 conf 目录里添文件，也不改别人的配置**。

### 6.1 先抄作业（必做，别猜）

```cmd
type E:\projects\RoamSync-runtime\nginx\roamsync.conf
findstr /n "include" D:\Software\nginx\conf\nginx.conf
```

看两件事：

1. **roamsync.conf 的结构**：完整 `server { }`，还是只有 `location` 片段。
   子路径共用 80 通常是后者——`deploy\nginx\cognition.conf` 是按片段写的；
   如果样板是完整 server 块，我们的也要按同样结构包一层。
2. **主配置怎么加载它**：通配（`include E:/projects/*/nginx/*.conf`）还是逐个文件 include。

### 6.2 放文件

```cmd
mkdir E:\projects\renzhifujia\nginx 2>nul
copy E:\projects\renzhifujia\deploy\nginx\cognition.conf E:\projects\renzhifujia\nginx\cognition.conf
```

- 主配置是**通配** include → 到这一步就完了，主配置一个字都不用改；
- 主配置是**逐个文件** include → 在主配置里**只加一行**，紧挨着 roamsync 那一行放：
  `include E:/projects/renzhifujia/nginx/cognition.conf;`
  改前先备份到自己目录：`copy D:\Software\nginx\conf\nginx.conf E:\projects\renzhifujia\backup\nginx.conf.bak`

### 6.3 记基线

```cmd
curl.exe -s -o nul -w "roamsync: %%{http_code}\n" http://10.100.128.129/roamsync/
```

RoamSync 走的是同一条链路，最合适做对照。记下状态码，reload 之后再打一次对比。

### 6.4 校验通过才重载

```cmd
cd /d D:\Software\nginx
nginx -t
```

只有 `syntax is ok` + `test is successful` 才执行 `nginx -s reload`。

**`nginx -t` 失败时绝不 reload**：说明文件有语法错误（多半漏了 `}` 或 `;`）。
此时线上仍是旧配置在跑，别人不受影响，改好再试。

### 6.5 回滚

删掉我们那个文件（`E:\projects\renzhifujia\nginx\cognition.conf`）；
如果改过主配置，把 `backup\nginx.conf.bak` 复制回去。然后：

```cmd
nginx -t && nginx -s reload
```

reload 是重载所有站点、耗时毫秒级。配置校验不通过时 Nginx 会拒绝切换，
**所以最坏情况是「我们的子路径没生效」，不会是「别人的站点挂了」**。

### 6.6 三个技术点

1. `location = /cognition` 必须 301 到 `/cognition/`，否则相对路径的基准目录会跑偏；
2. `proxy_pass http://127.0.0.1:8010/;` **结尾那个 `/` 是剥前缀的关键**，
   漏了应用会收到 `/cognition/api/...` 而匹配不上路由；
3. `client_max_body_size 200m`：书籍单份上限 100MB，Nginx 默认 1m 会 413。

前缀在第 2 点那层已经剥掉了，所以**应用侧 `.env` 的 `ROOT_PATH` 留空**——
填了不仅没用，还会让静态挂载失配、整站丢样式（见 §4 的红字）。

## 7. 端口放行与登记

**默认什么都不做。** 判断依据看现实，不去动防火墙：

- 别的项目已经能通过 `http://10.100.128.129/<路径>` 访问 → 说明 80 早已放行，
  **不需要新增任何规则**（加重复规则是在改共用环境，没必要）；
- 真访问不了 → 先确认不是 Nginx 没起，再找管理员确认端口策略，别自己加全局规则。

只想查证的话（只读命令）：

```cmd
netsh advfirewall firewall show rule name=all dir=in | findstr /i "80"
```

**不要放行 8010**：应用只绑 `127.0.0.1`，多开一个对外端口既是攻击面，
也违反了「不要影响共用环境」。

往登记表**追加**一行（记事本打开，在末尾回车再写，**别覆盖别人写的内容**）。
格式照抄表里已有的写法（本草灵诊 / ShopAgent / RoamSync），本项目与 RoamSync 同型：

```
项目名称：认知副驾    统一访问入口：http://10.100.128.129/cognition/    对外端口：80，共用统一 Nginx    内部应用：127.0.0.1:8010（前端与 API 同源）    项目配置：E:\projects\renzhifujia\nginx\cognition.conf
```

与仓库里已有的三条对照：

| 项目 | 对外 | 内部 |
|---|---|---|
| 本草灵诊 | `localhost:5173` | 后端 8080 / 前端 5173 / Ollama 11434 |
| ShopAgent | `10.100.128.129:8081` | 后端 18080 / 前端 8081 / Agent 18090 |
| RoamSync | 80 共用 Nginx，`/roamsync/` | 前端 3002 / API 8002 |
| **认知副驾** | **80 共用 Nginx，`/cognition/`** | **应用 8010（前端与 API 同一个进程）** |

端口上不与任何已有项目冲突：8010 不在 8080 / 8081 / 5173 / 18080 / 18090 / 3002 / 8002 / 11434 之列。
仍要用 `netstat -ano | findstr ":8010"` 确认没有未登记的占用。

## 8. 验收

```cmd
curl.exe -s -o nul -w "app:%%{http_code}\n"  http://127.0.0.1:8010/health
curl.exe -s -o nul -w "nginx:%%{http_code}\n" http://10.100.128.129/cognition/health
```

浏览器打开 `http://10.100.128.129/cognition/`，按顺序走一遍：

> **用无痕窗口或先 `Ctrl+Shift+R` 强刷**。Service Worker 会缓存旧版 JS，
> 第一次访问如果拿到的是改造前的脚本，表现就是「页面能开、接口全 404」，
> 很容易被误判成 Nginx 配错了。

- [ ] 首页正常，样式与图标在（F12 → Network，资源路径应是 `/cognition/assets/...`）
- [ ] 注册/登录成功，刷新后仍在登录态
- [ ] 对话页发一句话能回
- [ ] 知识库录入一条，列表出现
- [ ] 书架传一本小 txt，能打开阅读
- [ ] 注销 `stu1`，用 `stu2` 登录，服务仍在线（这条验证服务化是否真的生效）
- [ ] **别的项目没被动到**：回到 6.1 记的基线入口，状态码与改动前一致
- [ ] **别人占的端口仍正常**：`netstat -ano | findstr ":80 :8010"` 里 8010 只属于本项目进程

## 9. 更新线上代码

```cmd
deploy\update.bat
```

顺序是「备份 → 停 → 确认端口空出 → 拉代码 → 装依赖 → 起 → 等健康 → 自检」，
任一步失败会明确报出来并**中止**（备份失败绝不停服务；代码没拉到会先把服务拉起来再退出，
不留「停着不起来」的状态）。数据文件（`dev.db` / `data\` / `models\` / `.env`）不在版本库里，更新不会碰它们。

脚本里的「确认端口空出」不是多余的：`schtasks /end` 结束的是任务实例，个别情况下子进程
会活着继续占着 8010，新实例起不来、旧进程继续服务——表现正是「代码更新了但行为没变」。

更新后如果行为不对，**先重启服务再排查**，别只信 `--reload`（生产没开）。

### 9.0 一次性迁移：手工复制的目录 → git 仓库

判断依据只有一条：

```cmd
cd /d E:\projects\renzhifujia
git remote -v
```

- 输出两行 `origin ... knowledge-agent`（fetch / push 各一行）→ 已经是仓库，直接看 9.1。
- 输出 `fatal: not a git repository` → **这个目录是手工拷出来的**，`git pull` 永远不会成功。

2026-11-09 线上就是第二种。这种目录最坑的地方是它「看起来完全正常」：页面能开、库能读、
任务计划能跑，`deploy\update.bat` 也能跑到第 4 步才失败——现场只看到「更新失败」，
看不到「这里根本不是仓库」。历次「代码更新了但行为没变」有一半是这个原因。

在**管理员** cmd 里跑一次下面五条，把它变成真仓库：

```cmd
cd /d E:\projects\renzhifujia
git init
git remote add origin https://github.com/Curtainorange/knowledge-agent.git
git fetch origin
git checkout -f -B main origin/main
git status --short
```

- `.env`、`dev.db`、`data\`、`models\`、`.venv\` 全部是 git-ignored，**不会被覆盖**，也不会入库。
  只有**代码**被换成仓库里的版本——包括服务器上那些手工改过的地方（这正是想要的）。
- `-f` 是必须的：目录里已有的同名文件在 git 眼里是"未跟踪文件"，不加 `-f` 会直接拒绝。
- 跑完 `git status --short` 通常只剩几行 `??` 开头——那是**仓库里没有的本地文件**
  （`nginx\cognition.conf`、`logs\`、`data\` 等），**不要删**；只有确认是旧代码残留时才清掉。
- `deploy\update.bat` 现在会在**动手之前**先判这一条：不是仓库就直接退出并把这五条打出来，
  不会先停服再报错。`scripts\check_deploy.py` 同理，「版本」小节第一项就判。

变过来之后，以后所有更新都走 `deploy\update.bat`，不再拷文件。

### 9.1 服务器从哪拉代码、要不要配密钥

仓库 `Curtainorange/knowledge-agent` 是 **public**，所以**只拉不推的服务器不需要任何凭据**，
不必配 SSH key、也不必配 token：

```cmd
cd /d E:\projects\renzhifujia
git remote -v                                    rem 应指向 github.com/Curtainorange/knowledge-agent
git remote set-url origin https://github.com/Curtainorange/knowledge-agent.git
                                                 rem 上一条不是这个地址时才需要
git ls-remote origin HEAD                        rem 返回一个 sha = 网络通、不用登录
git pull --ff-only
```

**push 才需要凭据**（SSH key 或 PAT），服务器只读，不用管。

首次拉取常见的四个失败：

| 报错 | 原因与处理 |
|---|---|
| `Connection was reset` / 超时 | 服务器出不了外网。要么给 git 配代理 `git config --global http.proxy http://…`，要么在能上网的机器上 `git bundle create repo.bundle --all`，拷过去 `git clone repo.bundle` |
| 提示输入用户名/密码 | remote 指向的不是那个公开地址，或仓库被改成了私有。按上面的 `set-url` 改正 |
| `detected dubious ownership` | 目录属主与当前用户不一致：`git config --global --add safe.directory E:/projects/renzhifujia` |
| `local changes would be overwritten` | 服务器上有手工改过的文件。**不要** `git checkout -- .`，先 `git stash push -m "server-before-update" -- web app tests scripts deploy docs`（不要加 `-u`：`nginx\cognition.conf` 不在版本库里，会被一起收走） |

不要用 `--depth` 浅克隆：出问题要回滚到旧提交时没有历史可用。

### 9.2 只做核对、不动服务

```cmd
.venv\Scripts\python.exe scripts\check_deploy.py
```

只读检查：Python 版本、`AGENT_ROUTE` 版本够不够（确认拉到的不是旧代码）、配置项、
数据库连通与表数、**联网检索能不能真跑通**。加 `--no-network` 跳过联网探测（不花钱），
加 `--strict` 把提醒也当失败（当上线闸门用）。

它回答的是「服务器上现在跑的这份，到底行不行」，与 `pytest`（逻辑对不对）、
`harness_check.py`（边界有没有越）不重叠。

## 10. 常见故障对照

| 现象 | 原因 |
|---|---|
| `git remote -v` 报 `not a git repository` | 目录是手工拷出来的，没有 `.git`。`git pull` 永远失败——见 **9.0** 一次性迁移 |
| 打开子路径整页 404 | `proxy_pass` 结尾漏了 `/`，前缀没剥掉 |
| 页面能开、样式/接口全 404 | 浏览器缓存着旧版绝对路径的 JS → `Ctrl+F5` 强刷 |
| 413 Request Entity Too Large | 漏了 `client_max_body_size` |
| 502 Bad Gateway | 服务没起、端口不对，或工作目录错导致启动即崩（看 `logs\app.log`） |
| 登录后立刻掉线 / 重启即掉线 | `JWT_SECRET` 没配 |
| 语义检索退化、`embed_status=embed_failed` | 缺 `onnxruntime`/`tokenizers`，或模型目录不完整 |
| 上传大 PDF 报超时 | `proxy_read_timeout` 太短 |
| 两个人各起了一份，互相端口冲突 | 服务是机器级的，**只该有一份** |
| 更新后行为没变 | `schtasks /end` 后旧子进程仍占着 8010，新实例没起来 → `netstat -ano \| findstr ":8010 "` 查 PID；或浏览器缓存着旧 JS → `Ctrl+F5` |
| 联网找材料总回「这次没搜到」 | 跑 `scripts\check_deploy.py`：区分是出网被拦（白名单）、TLS 中间人证书、还是 `DEEPSEEK_BASE_URL` 指向了不支持 `/v1/tools/search` 的中转站 |
| 手动改 `logs\` 或 `dev.db` 提示没权限 | 服务以 LocalSystem 运行，文件属主是 SYSTEM；给该文件夹加 `Users` 的「修改」权限即可 |
| `nginx -t` 报错 | 粘贴时漏了 `}` 或 `;`。此时**没有 reload，线上仍是旧配置**，别人不受影响 |
