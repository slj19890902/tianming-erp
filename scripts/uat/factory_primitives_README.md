# Windows 源端前置接口：最小隔离测试包

这是合成测试包，不是 ERP 运行包。原实现和四个测试文件逐字节来自固定提交
`82f0a79c91577b9b295d8f55077fa6c21356ecc7`，没有裁剪实现或用替身代替它们。
新增 runner、配置和本说明的提交单列在 manifest 的 `harness_commit`。
独立交付回执提供两个提交、源码 ZIP 和离线依赖 ZIP 的 SHA256/大小；先从该回执
核对文件，再解包。包内自报的摘要不能替代独立交付回执。

## 范围、权限和结果含义

用户已批准：只在新建临时文件夹内调整访问权限、构造同目录的虚构目录链接，
使用虚构数据；不得改正式 ERP、上级目录权限或建立持久信任。
**第三方依赖安装须另获批准**，本包不会自动安装。不要扩大正式目录访问、
takeown、给旧父目录运行 icacls、设置 Git safe.directory 或寻找网络替代入口绕过拒绝。

此包只含 12 个原代码模块、4 个原测试、最小 runner/配置/说明及公开依赖清单。
不含业务数据库、附件、备份、邮箱/AI/备份凭据、真实发布身份、公钥信任配置、
ERP 主应用、前端、Git 历史或第三方运行库。4 个测试文件共 51 项；所有测试输入
在执行时生成。合成 Ed25519 身份和签名不被任何真实系统信任，不注册账号、
公钥、登录入口或持久访问；部分合成密钥暂存为 PEM，部分用当前测试用户 DPAPI
封装，均只在新测试目录中。不得向测试传入任何真实凭据或正式路径。

执行身份若为 CodexSandboxOffline，DPAPI 结果只证明该沙箱令牌能封装/解封自己
临时生成的合成值。即使全部通过，也**不证明工厂原用户或真实发布私钥可读取**。
本轮不执行正式签名、切换签发、运营凭据导出、备份恢复、停服或业务流量。

## 解释器、完整依赖和官方来源

要求 Windows x86_64 CPython **3.12.x**，因为原代码使用 `Path.is_junction`。
协调端报告已核验如下基础解释器为 3.12.13；本包仍会只读检查它，不安装或修改它：

`C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe`

若该路径在当前授权上下文不可达或是重解析路径，停止并回报，不替换入口绕过。
Python 官方来源：[Windows 发布页](https://www.python.org/downloads/windows/)。

全部测试运行依赖（不需要 ERP requirements 全集）：

| 包 | 固定版本 |
|---|---|
| pytest | 8.4.2 |
| cryptography | 50.0.2 |
| psutil | 7.2.2 |
| colorama | 0.4.6 |
| iniconfig | 2.3.1 |
| packaging | 26.3 |
| pluggy | 1.6.0 |
| Pygments | 2.21.0 |
| cffi | 2.1.1 |
| pycparser | 3.11 |

requirements.txt 对每个 Windows wheel 固定版本和 SHA256；
windows-wheel-audit.json 包含精确文件名、大小、官方 `files.pythonhosted.org` URL
及对 Windows CPython 3.12 生效的依赖。作者已下载并静态检查 wheel METADATA、
ZIP 完整性、官方 PyPI SHA256和完整依赖闭包，**没有在 Mac 执行 Windows wheel**。
PyPI 官方项目：[pytest](https://pypi.org/project/pytest/8.4.2/)、
[cryptography](https://pypi.org/project/cryptography/50.0.2/)、
[psutil](https://pypi.org/project/psutil/7.2.2/)。其余 wheel 的精确官方来源在审计 JSON 内。

交付的独立 wheelhouse ZIP 约 6 MB，仅含上述 10 个公开 wheel。依赖安装通过
`--no-index --require-hashes` 使用这个已校验目录，不访问包索引，不装进基础解释器。
无需安装新的 Python。若尚未获准安装依赖，只执行下面 Check 阶段。

## 完整 Windows 执行流程

只使用用户已经授予当前任务访问权的交付目录和新的任务父目录。以下参数都是
普通非秘密路径/摘要，不能填正式数据库、发布身份或凭据路径。请在**新开的
Windows PowerShell** 中执行，以便本进程 TEMP/PATH 调整不影响原有会话。

### 1. 先验证交付文件并解包到新的获准目录

独立交付回执中包含实际 ZIP 文件名和摘要。将下面路径/摘要替换成该回执中的值；
不从未核验 ZIP 内读取其自身应有摘要。已有目录一律保留，不覆盖解包。

```powershell
$ErrorActionPreference = 'Stop'
$CodeZip = '<已授权本地交付目录中的最小源码ZIP绝对路径>'
$CodeSha256 = '<独立交付回执中的源码ZIP SHA256>'
$WheelZip = '<已授权本地交付目录中的离线wheel ZIP绝对路径>'
$WheelSha256 = '<独立交付回执中的wheel ZIP SHA256>'
$ApprovedParent = '<用户已授权的新任务父目录绝对路径>'
$HarnessCommit = '<独立交付回执中的harness_commit>'
$SourceCommit = '82f0a79c91577b9b295d8f55077fa6c21356ecc7'
if (-not (Test-Path -LiteralPath $ApprovedParent -PathType Container)) { throw '父目录不可达，停止' }
if ((Get-FileHash -LiteralPath $CodeZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $CodeSha256) { throw '源码包摘要不符' }
if ((Get-FileHash -LiteralPath $WheelZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $WheelSha256) { throw '依赖包摘要不符' }
$Kit = Join-Path $ApprovedParent ('kit-' + $HarnessCommit.Substring(0,8))
$Wheels = Join-Path $ApprovedParent ('wheels-' + $HarnessCommit.Substring(0,8))
if ((Test-Path -LiteralPath $Kit) -or (Test-Path -LiteralPath $Wheels)) { throw '解包位置已存在，保留现场' }
Expand-Archive -LiteralPath $CodeZip -DestinationPath $Kit
Expand-Archive -LiteralPath $WheelZip -DestinationPath $Wheels
```

如果只能先取得源码包，可以暂不解包 wheelhouse，只进行下一步只读 Check。
未取得包或摘要不符时，不运行测试、不安装依赖、不自行拆改包。

### 2. 只读 Check：不安装、不生成测试身份、不修改 ACL

```powershell
& (Join-Path $Kit 'windows.ps1') -Action Check -TaskParent $ApprovedParent -ExpectedSourceCommit $SourceCommit -ExpectedHarnessCommit $HarnessCommit
```

若脚本执行策略拒绝，原样报告，不改机器/用户策略、不换执行策略绕过。Check
核对基础解释器、包内每文件摘要与两个固定提交以及父目录可达性；不代表依赖已装。

### 3. 仅在明确批准依赖安装后，创建任务专用 venv

```powershell
& (Join-Path $Kit 'windows.ps1') -Action Install -DependencyInstallationApproved -TaskParent $ApprovedParent -ExpectedSourceCommit $SourceCommit -ExpectedHarnessCommit $HarnessCommit -Wheelhouse $Wheels
```

脚本先核对 10 个 wheel 的大小和完整 SHA，才独占新建
`<ApprovedParent>\factory-synthetic-<harness前8位>`。基础 Python 用 `-I -B -m venv`
只在该目录内创建 `venv`；随后 venv Python 离线安装完整锁定依赖，并执行 pip check。
setup-temp、venv-create.log、dependency-install.log、dependency-check.log 全在本任务内。
安装前清理本子进程继承的 PIP_* 并设置 PIP_CONFIG_FILE=nul（严格匹配 Windows 的
os.devnull 小写值），禁用全局/用户/venv
pip 配置；`--isolated` 本身不足以禁止全局配置。安装和检查都要求 virtualenv，
并验证解释器 sys.prefix 正是本任务 venv。安装前还从该 venv 的 pip 读取实际有效配置，
只有配置为空才继续；诊断仅保存到任务 pip-config-check.log，不打印任何配置值。
任务父目录和输出路径先规范化再比较，
不得通过 `..` 或 Windows 短路径别名把输出放进不可变源码包。
没有更改基础解释器、全局 PATH、系统环境、原 ERP 或旧目录权限，也不注册服务/计划。
开关只用于防止误运行，不替代用户批准。失败目录不覆盖重试，不自动删目录。

### 4. 安装通过后运行 4 文件共 51 项原测试

```powershell
& (Join-Path $Kit 'windows.ps1') -Action Run -TaskParent $ApprovedParent -ExpectedSourceCommit $SourceCommit -ExpectedHarnessCommit $HarnessCommit
```

等效核心入口为本任务 venv 的：

```text
python.exe -I -B <Kit>\run_tests.py --work-root <Task>\execution --expected-source-commit <SourceCommit> --expected-harness-commit <HarnessCommit>
```

runner 核验包只含允许文件及摘要、依赖版本，要求全新 `execution` 目录。先清理
本子进程继承的 ERP/TM_ERP/PYTEST 配置，再设置 `ERP_ENVIRONMENT=test`、
`ERP_HOME_REHEARSAL=1` 及任务 temp 内的合成数据库/备份/密钥文件路径；不更改
Windows用户身份、USERPROFILE、APPDATA或DPAPI上下文。TEMP/TMP/TMPDIR和
Python tempfile 均指向本任务。Windows PATH只保留该解释器和实际系统工具路径，
保留并核对 SystemRoot，不伪造用户配置。

pytest 明确只收集四个原文件，使用 `--noconftest`，不加载ERP通用 conftest/业务模型，
禁用外部插件；pytest缓存、basetemp和全部unittest临时目录落在execution/temp。
basetemp只在本次独占新目录中使用，不接受旧目录。结果记录所有实际导入的
desktop_assistant模块路径，禁止从工厂ERP或别处导入同名模块，并在结束后复核源码未变。

ACL影响仅限测试临时生成的publisher/private-parent/job。junction目标也在同一任务内。
PowerShell“停服”测试中的查端口/身份/停止函数都是原测试自行生成的trace夹具，
没有调用真实维护脚本、真实ERP端口、工厂数据库或服务。测试不启动HTTP监听。

## 验收、回传和保留

Windows应为 **49通过、2跳过、0失败/错误**；两个跳过仅为POSIX symlink夹具和
非Windows拒绝检查。3项ACL/junction、4项合成DPAPI、3项PowerShell必须在Windows实际跑，
不能跳过后称全部通过。Mac的41通过/10跳过只算作者独立包验证。

结果在 `<Task>\execution\results`：windows-tests.xml、windows-tests.log、receipt.json。
回传执行身份名称、解释器/系统、依赖版本、两个提交、两个交付包SHA、通过/失败/跳过
及skip原因；仍注明SandboxOffline不代表原工厂用户。回传前检查日志只含合成诊断，
不要附临时PEM、publisher.json、DPAPI密文、整个temp/venv或任何真实配置。

任务根、日志、JUnit、receipt 和 pytest 失败目录保留；原 unittest 临时夹具按原测试
自身的 TemporaryDirectory/addCleanup 清理，失败时也可能清理，不承诺保留这些夹具。
runner不自动删除execution或整个任务。完整回收须待结果归档、人工结束/无进程引用后，
按任务所有权另行处理；不改权限强删，不清空旧机。工厂不修改源码，失败回传Mac唯一作者。

NAS可达并具有上传授权时，结果另存到独立交付回执指定的**新结果子目录**并回读SHA；
不可达就保存在上述本地results，不绕过网络限制。旧大包和交接单保留历史摘要，本小包
说明更新用户已批准的临时ACL/junction范围，不覆盖旧证据。

现有Mac签名运行包仍为源码3de3be32，未包含8838bb12、82f0a79c及本测试工具变更。
这个测试包不授权真实签名、正式停写、隔离、最终同步、系统服务安装或正式接管。
