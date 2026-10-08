# iCloud Drive Monitor

面向 macOS 的本机 iCloud Drive 观测与空间整理工具。它把 Finder 可见路径的文件系统活动、已落地文件的本机分配量和“移除本地下载”结果放到同一页，重点解决两个问题：

1. 当前 iCloud Drive 究竟在触及哪个 Finder 路径。
2. 哪些文件已经占用本机磁盘空间，以及如何在保留云端文件的前提下请求移除本地副本。

> [!WARNING]
> 这是一个 macOS 专用工具，需要管理员密码来启动本机监听服务。它不会上传文件名、文件内容、扫描结果或日志；全部通信局限于 `127.0.0.1`。iCloud Drive 的同步状态由 macOS 管理，本工具不承诺暂停、恢复或加速同步。

## 功能

- **按页面生命周期监听**：浏览器页面连接后才启动 `fs_usage`；关闭页面后停止监听子进程。
- **过滤至 Finder 可见路径**：只保留 `~/Library/Mobile Documents/com~apple~CloudDocs` 下的路径活动，跳过 `CloudDocs/session` 等内部会话位置。
- **解释常见操作**：页面内说明 `lstat64`、`getattrlist`、`RdData`、`WrData`、`rename`、`fsync` 等事件的含义与局限。
- **手动扫描本机驻留项目**：默认不遍历 iCloud Drive。只有点击“扫描本机元数据”才会进行元数据枚举。
- **尽量避免触发按需下载**：扫描线程启用 `IOPOL_MATERIALIZE_DATALESS_FILES_OFF`，并用 `SF_DATALESS` 判断本机是否缺少文件内容；无法启用保护策略时扫描直接停止。
- **可展开的本机文件树**：支持一级、二级、三级展开，显示各文件夹已确认的本机分配量，默认略过 0 B 项目。
- **层级批量选择**：文件夹左侧复选框选择其已下载子文件；局部选择显示半选状态，并向上汇总数量及预计占用空间。
- **Finder 定位**：悬停或键盘聚焦行后可“在 Finder 中显示”；处理结果中的路径也能直接定位。
- **移除本地下载**：一次最多处理 200 个文件。每个项目重新校验路径、inode、尺寸、修改时间、`SF_DATALESS` 状态和上传状态，然后请求 macOS 移除本地副本。云端文件路径不会被删除。

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 系统 | macOS，建议 13 Ventura 或更高版本 |
| 编译工具 | Xcode Command Line Tools，提供 `xcrun clang` |
| 运行时 | macOS 自带 Python 3 或可用的 `python3` |
| 权限 | 管理员密码，用于本地 `fs_usage` 服务 |
| 网络 | 启动后不需要网络访问；iCloud 自身同步仍由系统网络策略决定 |

## 快速开始

```bash
git clone https://github.com/youer1016/icloud-drive-monitor.git
cd icloud-drive-monitor
open "启动 iCloud Drive Monitor.command"
```

也可以在 Finder 中双击 `启动 iCloud Drive Monitor.command`。首次启动会：

1. 编译 `evict_local.m` 为本机辅助程序 `evict_local`。
2. 请求管理员认证，并在 `127.0.0.1:8766` 启动服务。
3. 打开 `http://127.0.0.1:8766`。

服务只绑定回环地址，局域网设备无法连接。

## 使用方式

### 观察当前路径活动

保持页面打开。表格会显示经过过滤后的文件系统记录，包括时间、进程、操作、分类和实际路径。

`bird` 是 iCloud Drive 的 CloudDocs 守护进程；实际下载或文件提供者工作也可能由 `fileproviderd` 发起。单条 `RdData`、`WrData` 或 `lstat64` 记录只说明某个文件系统操作发生过，无法独立证明“文件正在下载”。建议结合路径、连续事件和 Finder 状态判断。

### 查看已占用本机空间的 iCloud Drive 内容

点击“扫描本机元数据”。扫描使用目录枚举和 `stat` 元数据，不读取文件内容。扫描完成后：

- 文件夹大小为当前确认已落地子文件的 **APFS 分配量**；它与 Finder 逻辑大小可能不同，原因包括压缩和共享块。
- 默认只显示分配量大于 0 B 的项目。
- “未枚举”代表该文件夹处于云端占位状态，工具没有深入访问它。
- 扫描是一个时点快照。Finder 图标和系统状态可能晚于扫描结果变化。

### 在 Finder 中核对

对文件、文件夹或处理结果路径点击“在 Finder 中显示”。工具只会在明确点击时调用 `open -R`；定位动作不会读取文件内容。

### 移除本地下载

勾选文件或文件夹后，点击“移除所选本地下载”。确认框会说明预计释放空间与限制。每一项可能得到以下结果：

| 结果 | 含义 |
| --- | --- |
| 已移除 | macOS 已将文件变为无本地内容状态；iCloud 云端文件保留。 |
| 已无本地副本 | 提交前或提交后复查发现文件已是 `SF_DATALESS`；工具没有再次执行移除。 |
| 未移除 | 上传尚未完成、文件正在使用、被标记为不可驱逐、路径状态改变，或系统接口拒绝操作。页面会显示具体错误。 |

处理完成后可点击结果路径到 Finder 核对。请先用一个已确认上传的小文件测试。

## 安全模型与边界

### 不会自动发生的行为

- 打开网页不会扫描 iCloud Drive。
- 扫描不会打开文件内容。
- 扫描不会调用下载 API。
- 悬停行不会打开 Finder。
- 任何移除本地下载操作都要求页面中的显式确认。
- 本工具不会删除 iCloud 云端文件，也不会执行 `rm`、移动文件或清空废纸篓。

### 仍可能发生的系统行为

macOS 和 Finder 可在工具未访问某个文件时自行继续或发起同步。访问无本地内容的文件通常会触发内容实体化；因此请避免为了验证状态而双击、预览或用其他应用打开文件。

Apple 将无本地内容的文件称为 *dataless file*。`SF_DATALESS` 是本项目判断本地内容缺失的核心依据；Apple 也建议通过 `stat` 或 `getattrlist` 检查该标志，并在遍历前关闭自动实体化策略。[TN3150：Getting ready for dataless files](https://developer.apple.com/documentation/technotes/tn3150-getting-ready-for-data-less-files)

### “macOS 未确认 iCloud 归属”是什么意思

该报错来自 `NSURLIsUbiquitousItemKey`。它表示 macOS 没有将当前 URL 作为可通过 `FileManager.evictUbiquitousItemAtURL:` 操作的 iCloud ubiquitous item 返回。常见情况包括：

- 文件在扫描后已转为云端占位状态。
- 文件由较新的 File Provider 路径管理，传统 ubiquitous 属性没有给出肯定结果。
- 同步状态仍在转换，Finder 图标与元数据暂时不同步。
- 文件已移动、替换或处于系统暂不允许驱逐的状态。

这条信息不能单独证明文件离开了 iCloud，也不能单独证明它仍有完整本地副本。工具会在收到此错误后再次检查 `SF_DATALESS`；如果已无本地副本，会显示“已无本地副本”。若状态仍无法确认，工具保留文件不动，并提供结果路径的 Finder 定位按钮。[Apple 对 ubiquitous 属性的定义](https://developer.apple.com/documentation/foundation/filemanager/isubiquitousitem%28at%3A%29?language=objc)

## 实现设计

```text
浏览器页面
  ├─ EventSource /events
  │    └─ 本机 fs_usage → 提取 Finder 可见 iCloud Drive 路径 → 表格
  ├─ POST /api/scan/start
  │    └─ 禁止自动实体化 → scandir + lstat/stat → SF_DATALESS / blocks → 文件树快照
  ├─ POST /api/reveal
  │    └─ 快照路径复核 → open -R → Finder 定位
  └─ POST /api/evict/start
       └─ 身份复核 → 原生辅助程序 → 上传状态 / 固定标记 / FileManager 驱逐
```

### 扫描路径

`monitor_server.py` 先用 `setiopolicy_np(IOPOL_MATERIALIZE_DATALESS_FILES_OFF)` 建立线程级保护，再调用 `os.scandir()` 与 `DirEntry.stat(follow_symlinks=False)`。扫描只保存普通文件的相对路径、已分配块数、设备号、inode、逻辑尺寸和纳秒级修改时间。

移除前会重新核对这些身份字段，避免把扫描完成后同名替换的文件当作原目标处理。父目录也会检查为已实体化目录；存在路径变化时操作中止并要求重新扫描。

### 监听路径

原始 `fs_usage` 输出同时包含用户路径和 CloudDocs 内部工作路径。工具从每行中提取 Finder 可见 iCloud Drive 根目录起始的路径，再丢弃无法确认在该根目录中的记录。页面会把写操作标为“写入候选”，避免把任何单个事件描述为确定下载。

### 驱逐本地副本

`evict_local.m` 使用 Foundation 的 `evictUbiquitousItemAtURL:error:`。Apple 将该 API 定义为移除存储在 iCloud 中项目的本地副本，云端项目继续保留。[Apple API 文档](https://developer.apple.com/documentation/foundation/filemanager/evictubiquitousitem%28at%3A%29)

辅助程序会先确认：

1. 路径仍是普通文件。
2. 当前版本的上传状态为完成。
3. 读取到的固定下载标记可以安全处理。

“保留下载”标记使用 `com.apple.fileprovider.pinned#PX` 扩展属性。它属于实现层细节，未见 Apple 面向第三方的稳定公开契约，因此工具会在驱逐失败后尝试恢复原值，并把结果交给用户在 Finder 中核对。父文件夹的固定下载策略可能阻止子文件驱逐；项目不会自动改动整个父文件夹。

## 试验记录：哪些方案被放弃，为什么

这个工具经过几轮诊断后才形成现在的边界。下面保留关键技术决策，方便复核和后续维护。

| 尝试 | 观察到的问题 | 当前处理 |
| --- | --- | --- |
| 直接从 `bird` 日志推断下载文件 | 日志存在 `<private>`、内部容器标识和缺失的用户可见路径；无法稳定映射到 Finder 项目。 | 用 `fs_usage` 过滤到 Finder 可见 iCloud Drive 路径；将其视为“路径活动”，不宣称等于下载。 |
| 把 `CloudDocs/session/containers/...` 作为结果展示 | 这些是守护进程的会话工作目录，用户无法据此判断自己的文件夹或文件。 | 仅保留 `~/Library/Mobile Documents/com~apple~CloudDocs` 下的可见路径。 |
| 用普通递归统计本地空间 | 对按需下载项目的访问可能让 macOS 实体化文件或文件夹，正好违背“不要下载 iCloud 内容”。 | 先关闭 dataless materialization；遇到云端占位文件夹标为未枚举。 |
| 用文件逻辑尺寸判断是否已下载 | 占位文件也可能有逻辑长度；逻辑大小无法代表本机实际占用。 | 结合 `SF_DATALESS` 与 `st_blocks * 512`，前者判断内容缺失，后者估算 APFS 分配量。 |
| 用 `os.getxattr` 管理“保留下载”标记 | 某些 Python 发行版缺少 `os.getxattr`，导致移除流程在系统调用前失败。 | 将扩展属性读取、移除和失败恢复迁移到 Objective-C 原生辅助程序。 |
| 仅靠 `NSURLIsUbiquitousItemKey` 决定是否已下载 | 部分 File Provider 路径会报告否，且该属性的含义是 iCloud ubiquity 归属，不是本地内容状态。 | 以 `SF_DATALESS` 判断本地内容状态；ubiquity 属性仅作为调用 Foundation 驱逐 API 的安全门槛。 |
| 自动执行批量驱逐 | 文件可能有未上传修改、被使用，或在扫描后变化。 | 用户确认后逐项执行；限制 200 项；每项重新检查身份和状态；页面逐项报告。 |

## 开发与验证

编译和静态检查：

```bash
python3 -m py_compile monitor_server.py
node --check app.js
node --check tree.js
xcrun clang -fobjc-arc -framework Foundation evict_local.m -o evict_local
zsh -n "启动 iCloud Drive Monitor.command"
```

建议在不含 iCloud 文件的临时测试目录验证树界面、选择状态和 Finder 定位行为。真实 iCloud 文件的驱逐属于系统集成行为，应该先从一个已上传的小文件开始人工验证。

## 项目结构

```text
.
├── monitor_server.py              # localhost HTTP/SSE 服务、扫描、路径校验
├── evict_local.m                  # Foundation 驱逐辅助程序
├── 启动 iCloud Drive Monitor.command # 编译、认证、启动、打开浏览器
├── index.html                     # 页面结构
├── app.js / ui.css                # 路径活动界面
├── tree.js / tree.css             # 文件树、选择和处理结果
├── README.md
├── LICENSE
└── .gitignore
```

## 已知限制

- 工具面向 macOS，依赖 Apple 私有或半私有的系统行为，未来系统版本可能改变结果。
- iCloud Drive 的下载、上传、云端占位和 Finder 图标更新均为异步过程；结果应视为扫描时刻的证据。
- 本工具不会列举处于未枚举云端文件夹中的内容，借此避免主动访问该文件夹。
- `fs_usage` 需要管理员权限，且个别记录可能由 `bird`、`fileproviderd` 或其他系统进程产生。
- 点击文件、预览文件、第三方应用访问文件，都可能独立触发系统下载；本项目无法阻止这些外部行为。
- “保留下载”扩展属性没有稳定的公开 API 承诺，处理失败时应以 Finder 显示为准。

## 贡献

欢迎提交 issue 或 pull request。请勿在 issue、截图或日志中发布个人目录、文件名、Apple ID、同步令牌或完整 `fs_usage` 输出。

## 许可证

[MIT License](LICENSE)。
