# 茉莉 · 云运动

校园跑步 App「云运动」的第三方桌面客户端，由早前的「蔚园运动」演变而来。

界面基于 PyQt6 自绘，目前仅发布 Windows 端；代码中部分 UI 与工具函数借助 AI 生成，
可能存在冗余，欢迎指出。理论上支持所有使用云运动平台的学校（学校列表取自官方公开接口），
但未在全部学校实测，若你的学校不可用，欢迎提 Issue。

## 下载安装（普通用户）

1. 到 [Releases](../../releases) 下载 `moli-win-Setup-v1.0.exe`
2. 双击安装，可选择安装位置，勾选「创建桌面快捷方式」
3. 首次启动会被 Windows SmartScreen 拦截 → 点「仍要运行」
   （程序未签名，属正常现象；如不放心可自行从源码构建）

> 安装包含独立运行环境，无需安装 Python。
> 账号、轨迹、照片等数据仅保存在本机。

## 从源码运行（开发者）

```bash
python -m pip install -r requirements.txt
python moli.py
```

需要 Python 3.10+、Windows。

不想开界面也可以直接用命令行：

```bash
python cli.py login --user <学号>
python cli.py run --distance 1200 --fast
```

## 功能

- 📱 桌面端手机界面（PyQt6），登录信息本机保存、重开自动登录
- 🔐 账号、轨迹、照片等数据仅存本机，不上传第三方服务器
- 🏃 跑步任务提交：慢速（后台执行 + 进度轮询）/ 快速（同步完成）
- 📊 运动记录：按月分组、达标印章，点击查看速度着色轨迹详情（高德地图）
- ✏️ 跑区轨迹绘制：地图逐点绘制 → 闭合/里程校验 → 保存本机
- 😀 人脸照片管理：多张添加、提交时自动轮询携带
- 🗺️ 地图需自备高德 Web端(JS API) Key，在应用内「设置」填写

## 常见问题

**Q：为什么需要自己申请高德 Key？**
A：地图轨迹展示依赖高德 JS API，Key 与账号绑定，无法内置分发。

**Q：我的学校登录失败怎么办？**
A：先确认学校在官方接口列表中；若仍失败，请提 Issue 并附上学校名称。

**Q：会被学校检测到吗？**
A：本项目不讨论、也不保证任何规避检测的能力，请自行评估使用风险。

## 本次更新 2026-09-24

**1. cli 登录支持接管已有会话**

正常登录不变（`python cli.py login --user <学号>`）。新增三个参数，可跳过登录、
直接复用一份已有会话（例如你自己手机上产生的 token）：

```bash
python cli.py login --user <学号> --token <token> --uuid <64位设备号> --device-name "vivo(V2366GA)"
```

也可只在普通登录时指定设备身份：`--uuid` / `--device-name` 单独使用即可。

**2. 新增真机调试助手 `devinfo.py`（可选，面向开发者）**

独立脚本，不依赖本项目其他代码，通过 adb 连接自己的手机：

- 读取手机的 deviceName / sysEdition / deviceId
- 从手机日志中提取登录 token
- 实时查看手机与学校平台交互的明文请求/响应，便于排查接口问题

使用前需开启手机 USB 调试：

1. 设置 → 关于手机 → 连续点击「版本号」7 次，开启开发者模式
2. 设置 → 开发者选项 → 打开「USB 调试」
3. 数据线连电脑，手机弹窗「允许 USB 调试」→ 勾选一律允许
4. 电脑装 adb（没有也没关系，脚本会引导自动下载 Google 官方 platform-tools）

运行：

```bash
python devinfo.py
# [1] 取值：提取登录 token
# [2] 抓包：实时查看请求/响应明文
```

> 提示：日志包含账号等敏感信息，请勿截图外传；调试完可执行 `adb logcat -c` 清除。

## 目录结构

```
moli.py            入口（GUI）
cli.py             命令行入口（无 GUI，直接调用业务层）
devinfo.py         真机调试助手（可选，需 adb，与客户端本体无关）
bridge.py          JS ↔ Python 调用层
api.py             会话与轨迹校验
services.py        业务服务层
upstream.py        上游协议 / SM2 / SM4 加密
track_gen.py       轨迹生成
static/index.html  UI 页面（QWebEngineView 承载，bridge.py 负责 JS ↔ Python 通信）
```

## 项目状态

逆向自 APK 公开网络协议，仍在持续优化中：

- [x] Nuitka 打包 + Inno Setup 安装包
- [x] 上游接口经真机反编译逐端点核对
- [ ] 刷脸跑步实测（无测试账号，逻辑已对齐反编译行为）
- [ ] 轨迹生成算法拟真度调优
- [ ] 更多学校实测
- [ ] 清理 AI 辅助生成代码中的冗余（欢迎 PR）

欢迎 PR 与 Issue。

## 相关项目

- [Zirconium233/yunForNewVersion](https://github.com/Zirconium233/yunForNewVersion) —— 同类开源项目，思路相近、实现不同，推荐一看。

## 免责声明

本项目**仅供学习交流与技术研究**。逆向内容基于公开网络协议，
相关接口、数据与服务归平台及学校所有。请勿用于任何商业用途、
代跑作弊或其他违反校规校纪与法律法规的行为，由此产生的后果与作者无关。
使用即代表你已阅读并同意以上条款。
