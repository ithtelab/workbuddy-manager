# 发布流程（维护者手册）

上游 `workbuddy2api` 的**公开地址自 2026-09-23 起不可用**。维护者手上保留着完整的
源码副本，继续维护；**不要把上游代码提交进本仓库，也不要另开公开镜像** ——
用户侧的源码来自我们的 Release 包（见下）。

## 上游停更之后

- **发版前不再查外部上游**：上游由本项目维护，没有独立的远端提交可对。
  `deploy/check-upstream.sh` 只提示这一点并以 0 退出；需要验证的是补丁与
  Go 测试，不是 git 提交。
- **源码从哪来**：维护者手上留存了一份**最后版本的源码归档**（HEAD 快照，
  287 个文件，含 `LICENSE`）。它是完整的、可构建的；但原克隆是 blob-less
  部分克隆，**逐版本历史无法完整还原**，所以归档以「单次导入提交」的形式保存，
  不能 checkout 出中间某次提交的源码。
- **源码怎么到达用户**（**不要**把上游代码提交进本仓库，也不要另开公开镜像）：
  上游源码走 Release 包：
  1. 把最新的源码放进归档目录（覆盖对应文件；手上没有新源码就跳过这步）；
  2. `python dev/pack_upstream_src.py <源码目录> -o /tmp/upstream-pack --stamp "本次改了什么"`
     —— 打成 `workbuddy2api-src.tar.gz`（自动排除 `.git` 与 `config.json`/`auths`/`data`）；
  3. `gh release upload upstream-src /tmp/upstream-pack/workbuddy2api-src.tar.gz --clobber`
     —— 覆盖到**固定的载体 Release**（tag `upstream-src`，**必须是 pre-release**：
     否则它会成为 `releases/latest`，把面板的更新检查带偏）；
  4. 之后任何一次面板发版，CI 都会把它塞进发布包的 `upstream/`（取不到只告警、不
     阻断发布），用户装/更新时就用它。
- **改动上游代码**：改本地那份源码（`/opt/workbuddy2api`），
  然后 `docker compose up -d --build`。改的是别人的 MIT 代码，需保留其
  `LICENSE` 与版权声明。

### 我们自己改上游的那条路（补丁 + 重打包）

上游停更之后，「上游代码的改动」都由我们提出并留在 `dev/upstream-patches/*.patch`
里，**补丁是唯一事实来源**；本地改完就直接改源码目录的那种做法不再用于需要随包的
功能（那样改动只存在于某个人的机器上，下次重打包就丢了）。

1. 改代码：从**当前 `upstream-src` 资产**解开一份干净树（`tar xzf` 即可，别在
   本地运行目录上改），在里面写改动与 Go 用例；
2. 出补丁：`git init` 一个只含基线的仓库 → 改完 `git add -N` 新文件 → `git diff`
   存成 `dev/upstream-patches/000N-<名字>.patch`（**含测试文件**，0001/0003 都是这个
   体例；后续补丁只描述自己的增量，不要重述前面几个补丁）；
3. 验补丁：在一份**未被该补丁改动过**的资产基线上 `git apply --check` 通过、
   `apply_upstream_patches.py` 报 `applied:`、再跑一次报 `already applied:`。
   源码树里会生成 `.wb2a-patches.json`（补丁名 → 字节哈希）：后续补丁改写前面补丁的
   新增行时（0009 → 0010 就是），按内容判定会误报「没打」，清单记录的是事实；
4. 重打包：`dev/pack_fixed_upstream_src.py <那份树> -o <输出> --stamp '<一句话>'`
   —— 它会按序号依次应用全部补丁（已应用的会跳过），再打成 `workbuddy2api-src.tar.gz`；
5. 验收：解开新包 `go build ./...` + `go test ./...`（必要时加 `-race`）、
   包内 `scripts/` 的 Python 用例，然后
   `gh release upload upstream-src <包> --clobber` 覆盖载体 Release；
6. 记录：上传后核对资产的 **size 与 sha256** 与本地一致，并把「基线摘要 → 产出摘要」
   写进该功能自己的 docs（如 `docs/random-daily-tasks.md`、`docs/credit-exhausted-cooldown.md`）。

**顺序要求**：重打包必须在**当次面板发版之前**完成 —— CI 是从载体 Release 取
`upstream/` 塞进发布包的，晚一步设备装到的就还是旧上游。面板侧只加字段不算完成：
旧上游会忽略未知字段，用户会看到「保存成功但没生效」。

**CI 的发布门禁**（2.2.1 起）：发布作业会把随包上游当成一次真实构建来验 ——
`apply_upstream_patches.py` 必须报「补丁数 = 文件数，且没有现场新应用的」，
然后 `go build ./...`、`go test ./...`、对**补丁动过的文件**跑 `gofmt -l`，
并校验 `server/main.py` 的 `version=` 与 tag 一致。以前只跑 Python 测试，
上游补丁的缺陷可以一路进 Release。

- **`adapt(upstream)` 提交**：有可访问的上游代码要跟时照旧写——把核对过的上游
  提交号写进提交信息，供下次对照。

### 仍然要守的老规矩

管理端与上游之间有两条耦合路径 —— 即使上游停更，**改上游代码时**这两条依然成立：

1. **转发路径**（网关 `/v1/*` → 上游）：改到**入站协议/响应形状**就必须同步改面板。
2. **直连路径**（管理端**绕过上游直连腾讯**）：扫码登录、签到、查积分、地区注册、
   trial、探测。这条路与上游各写一份参照实现，**改一边不会自动改另一边**。

第 2 类最容易漏：两边接口都返回 200，行为却已经不一致。

## 更新日志的写法：写给用户，不是写给同事

`CHANGELOG.md` 的每一条都会**原样出现在用户看得见的地方**：GitHub Release 页面
的正文（CI 从 CHANGELOG 抽取）、应用内「设置 → 更新日志」页。读者是**使用者**，
不是维护者。

这个坑反复踩过：修完一个自己引入的缺陷后，很容易顺手把排障过程写进去 ——
函数名、模块路径、「我错在哪」「已做反向验证」「用例变红」，以及大段的设计权衡
推演。这些对用户毫无价值，还会把「升级后我要做什么」这个真正有用的信息淹掉。
1.0.42 ~ 1.0.49 全线存在这个问题，所以现在有测试守着（见下）。

**写**：

- 用户会遇到什么现象、升级后有什么变化
- 引用报错时**照抄界面上那句原文**（用户就是拿它来搜的）
- 用户要动手的东西：环境变量、配置项、命令
- 「为什么这么设计」里**影响用户决策**的那部分（如「上限不再跟随上游配置，
  改用环境变量 X」）

**不写**：

- 函数名、模块路径、变量名、测试名、内部术语（事件循环、线程池……）
- 排障过程与自我检讨（「根因是我……」「已做反向验证」）
- 代码评审式的取舍推演（内部权衡放代码注释，提交信息里可以写）

**判据**：一个只用产品、不读代码的人，能否看懂这条对他有什么影响。

**唯一的例外**：用户界面上的**报错原文**要照抄（含内部术语也没关系）——
守卫测试按「该词是否出现在用户可见的文案里」自动放行。

守卫在 `server/tests/test_changelog.py::ChangelogIsUserFacingTest`：扫全部版本段落，
检测内部标识符、排障叙事、以及未被用户文案引用的框架术语。写错了跑测试就会报出来，
**发版前那次 `unittest discover` 是最后一道闸**。

## issue 回复的写法：同样写给用户

上面那条规矩同样适用于 issue 回复，而且更严：回复挂在**公开** issue 下面，读者不只是
报告者本人 —— 更多是后来搜到自己那条报错、点进来的普通用户。他想知道的只有两件事：
**这问题修了没有**、**我要不要做什么**。

我的惯性正好相反：写回复时刚写完代码和提交信息，顺手把同一套东西搬了过来 —— 内部
函数名、「我上一轮引入的错」「已验证 N 条单测」、以及报告者没问到的边界推演。报告者
看得懂，搜进来的用户一脸茫然。#45、#46 两条最典型，原文长这样：

> 三条建议都采纳了（commit `f20e6f9`，下个版本生效）……抽出 `keysvc.model_allowed`，
> 调用侧与列表裁剪都用它……**这里有个反直觉的坑，差点做成假警报**……
> 验证：全量 1222 条通过；e2e 全过；浏览器验收 14 项全过。

同一件事，写给用户的版本：

> 已经改好了，**下个版本（1.0.61）生效**，不需要你做任何操作。
>
> 白名单现在会同时**收窄 `/v1/models` 返回的列表**，与调用时的放行判据完全一致
> （此前只管调用、不管列表，两者会对不上：客户端看到「能选」，选中却报「模型不在
> 密钥白名单内」）。在「密钥」页填写白名单时输错了名字，输入框下方会**当场提示**，
> 不用等到调用失败才发现。
>
> 两处容易踩的边界也一并处理了：白名单里写**别名**（设置页「模型映射」里配过的名字）
> 会保留在列表里；`cn:` / `global:` 前缀可写可不写。模型清单还没取到时不会误报，
> 只提示能确定的部分。

**写**：

- 一句话结论：**修了没有**；修了就说清哪个版本生效（写到具体版本号，别只说「下个版本」）
- 用户要动手的步骤 —— 在哪个页面点什么、填什么
- 会影响用户的行为变化，尤其是「升级后你会看到什么不一样」
- 报告者的分析确实帮上忙时，一句致谢；不必逐条点评

**不写**：

- 内部标识符：函数名、模块路径、字段名、内部接口路径、提交哈希
- 排障过程与自我检讨（「差点做成假警报」「一个自己踩的坑，记一下」）
- 测试与验收清单（多少条通过、做了哪些反证）—— 那是提交信息的位置
- 报告者没问到的边界情况的推演；确有必要就写进文档，回复里一句带过

**判据**：一个搜到这条 issue、但不读代码的用户，能否看懂「我受不受影响、要不要做什么」。

**长度**：结论 + 步骤，十行以内。回复越长，上面那条判据越难得证。

**例外**（与更新日志同一原则）：界面上的**报错原文**照抄，含内部术语也没关系，
用户就是拿它来搜的。

### 口吻：像维护者本人在说话，别像客服

同一条信息，措辞不同读起来差别很大。要避免的是那种「模板腔」——它对用户没有
任何信息量，还会把真话稀释掉：

> ❌ 感谢您的反馈！我们已经修复了该问题，希望这能帮助到您。需要注意的是，……
> 如有任何疑问，请随时与我们联系。

同样内容，人话版：

> 修好了，1.0.63 里。只读账号现在跟管理员看到的是同一份积分；查不到实时值时会
> 标「上游快照」，也不会再标红了。有问题再开。

几条具体的：

- 别用「感谢您的反馈 / 希望有帮助 / 如有疑问随时联系 / 给您带来不便」这类句式
- 别用「首先……其次……最后」，也别把两句话硬拆成项目符号（项目符号留给真清单）
- 少用加粗和破折号：一条回复里最多一两处加粗，其余靠句子本身说清
- 不必复述用户已经写过的东西，直接说结论
- 「有问题再说 / 有情况回一声」是自然的收尾，不是敷衍

`dev/check_issue_reply.py` 是发帖前的自检：草稿存成文件跑一遍，它会指出内部标识符、
测试清单、检讨式叙事、客套模板腔和超长段落（用 `--issue` 传入报告原文，可豁免你
引用的那几句）。
规范本身由 `server/tests/test_issue_reply_style.py` 钉着 —— 上面那条真实回复当反例，
它必须报出来；改好的短回复必须放行。

## 发版步骤

```bash
# 1) 改版本号与更新日志（「未发布」→「[x.y.z] - 日期」）
#    server/main.py 的 version、CHANGELOG.md
python -m unittest discover -s server/tests -t .   # 全绿才继续

# 2) 提交并推送
git add -A && git commit -m "chore(release): vX.Y.Z"
git push origin main

# 3) 打 tag（CI 会构建并创建一个**草稿** Release，CI 不发布）
git tag vX.Y.Z && git push origin vX.Y.Z

# 4) 等 CI 完成后，下载**最终产物**核对
gh release download vX.Y.Z --repo ithtelab/workbuddy-manager \
  --pattern 'workbuddy-manager-vX.Y.Z.tar.gz' --dir /tmp/vX.Y.Z
bash deploy/verify-release.sh /tmp/vX.Y.Z/workbuddy-manager-vX.Y.Z.tar.gz
#    → 打印本地 sha256，与 Releases 页面「产物校验（sha256）」里的值比一眼
tar tzf /tmp/vX.Y.Z/workbuddy-manager-vX.Y.Z.tar.gz | head -5      # 抽查包结构
#    再解开看 .version 与 server/main.py 的版本号

# 5) 给旧版更新器补过渡签名（v1.0.85 及更早仍强制验签；私钥只在维护者本机）
cd /tmp/vX.Y.Z
ssh-keygen -Y sign -f ~/.ssh/workbuddy-release -n file workbuddy-manager-vX.Y.Z.tar.gz
gh release upload vX.Y.Z workbuddy-manager-vX.Y.Z.tar.gz.sig \
  --repo ithtelab/workbuddy-manager
#    验一下再继续（用仓库里记录的公钥，或 update.py 里内嵌的那把）：
#    printf 'release %s\n' "$(cat ~/.ssh/workbuddy-release.pub)" > allowed_signers
#    ssh-keygen -Y verify -f allowed_signers -I release -n file \
#      -s workbuddy-manager-vX.Y.Z.tar.gz.sig < workbuddy-manager-vX.Y.Z.tar.gz

# 6) 核对无误后**发布**（草稿对用户不可见，这一步才让版本生效）
gh release edit vX.Y.Z --repo ithtelab/workbuddy-manager --draft=false
```

关键的一条：**草稿不发布，用户永远看不到这个版本** —— 面板与更新器的版本检查都读
`releases/latest`，而它会跳过草稿。草稿这一步是 issue #127/#129 之后加的：早先 CI
直接把 Release 公开发布，产物还在上传时点「一键更新」就会拿到半成品或 404。现在
用户侧看到的一直是上一个**已发布且产物齐全**的版本。

2026-10-14 起**新版本更新器不再要求签名**：只核对发布页给的 sha256 与文件大小
（发布说明里就印着那个哈希，工作流自动附上）。但 `.sig` 仍要按第 5 步手工补 ——
停在 v1.0.85 及更早的更新器强制验签，漏掉就会让这批老部署无法升级。为什么这样
过渡、私钥如何保管，见 [release-signing.md](release-signing.md)。

## 发布包字节的可复现性

CI 打包时固定了文件排序、mtime 与属主，并让 gzip 不写时间戳（`gzip -n`）。同一份
暂存内容重复打包会得到同一个 sha256 —— 这是发布说明里的哈希能作为承诺的前提。
改打包命令时不要退回普通的 `tar czf`（它会把构建时刻与属主写进字节流）。

## 发布包里放哪些文档

发布包只装**用户文档**（`docs/` 下的 9 篇：使用指南、功能一览、界面预览、开发与架构、
安全说明、API Token、账号出口线路、安全审查报告、发布包签名），外加 `docs/images/`
里的截图。名单在 `.github/workflows/release.yml` 的 `docs-pack` 段，由
`server/tests/test_docs_packaging.py` 双向守着（README 链到的必须在名单里；名单里
不许有 README 链不到的），打包时还有一道反向核对（包内 `docs/` 出现计划外文件即失败）。

**维护者/开发的文档留在仓库里、不进包**：发布流程、签名密钥管理、i18n 说明、
命名兼容笔记、上游修复记录、功能提案、设计与实现方案。理由两条：用户在安装目录里
翻到这些没有用；更要紧的是「内部细节被写进这些文档 → 静默分发给所有部署，收不回来」。
想让一篇文档进包，在 README 里链一下就够了 —— 它会自动被算进用户文档集合；
反过来，若某篇用户文档链到了内部文档，那篇内部文档也会被判成用户文档（测试会要求
把它加进名单），要么链、要么不链，别留个死链给用户。

> 已经发布出去的旧包不会因此改变（发布包是快照）：这份名单只对**下一次及以后**的
> 发版生效。

## 多人协作时的分工

仓库有 write 权限的协作者**可以直接推 tag**，推了就触发 CI 建**草稿** Release ——
但**发布**这一步只在维护者手上（`protect-tags` 规则集把 tag 创建限制给管理员角色，
发布草稿也只有维护者会做）。所以协作发版的分工是「**协作者准备，维护者核对并发布**」：

**协作者可以独立完成的部分**

```bash
# 改 .version 与 server/main.py 的 version、CHANGELOG.md 加段落
python -m unittest discover -s server/tests -t .   # 全绿
# 走 PR 合入 main，再推 tag
git tag vX.Y.Z && git push origin vX.Y.Z
```

**维护者收尾（核对 + 发布）**

```bash
gh release download vX.Y.Z --repo ithtelab/workbuddy-manager \
  --pattern 'workbuddy-manager-vX.Y.Z.tar.gz' --dir /tmp/vX.Y.Z
bash deploy/verify-release.sh /tmp/vX.Y.Z/workbuddy-manager-vX.Y.Z.tar.gz  # 与发布页哈希对一眼
tar tzf /tmp/vX.Y.Z/workbuddy-manager-vX.Y.Z.tar.gz | head -5              # 抽查包结构
# 解开看 .version / server/main.py 的版本号之后发布：
gh release edit vX.Y.Z --repo ithtelab/workbuddy-manager --draft=false
```

**协作者推完 tag、维护者还没核对之前，用户侧什么都看不到**：CI 建的是草稿，而版本
检查读的 `releases/latest` 会跳过草稿 —— 不会出现「点了更新却拿到半成品」的窗口期。
核对无误、发布之后版本才对用户生效。

> tag 与 main 分支都有保护规则（见下节）：协作者不能创建/删除 `v*` tag，
> 也不能直接往 main 推。这不是不信任，而是让「能改代码」与「能发布对用户生效的
> 版本」保持分离。

## 仓库保护规则

用 **ruleset**（不是旧的 branch protection）配置，因为 tag 保护只有 ruleset 支持。

| 规则 | 作用 |
|---|---|
| `protect-tags` (id 23464783) | `refs/tags/**` 的创建/删除/移动仅 admin 可做 |
| `protect-main` (id 23465105) | main 禁止删除与强推，且**必须走 PR**（至少 1 个批准） |

```bash
# 查看
gh api repos/ithtelab/workbuddy-manager/rulesets \
  --jq '.[] | "\(.id) \(.name) \(.target)"'
```

### bypass_actors 的 actor_id 是未公开的映射，别猜

`bypass_actors` 里的 `actor_id` 对 `RepositoryRole` 而言**没有被 GitHub 官方文档
记录**，而且**不是顺序编号**——按直觉填会出事：

| 角色 | actor_id |
|---|---|
| maintain | 2 |
| write | 4 |
| **admin** | **5** |

（有人按「read=1, triage=2, write=3, maintain=4, admin=5」推断后填了 4，
结果**任何 write 权限的人都能绕过**规则。）

这里用的是 `5`（admin）。这个值可以用 GraphQL 直接把角色名读出来核对——
REST 接口只回显整数，GraphQL 会给出 `repositoryRoleName`：

```bash
gh api graphql -f query='{ repository(owner:"ithtelab", name:"workbuddy-manager") {
  rulesets(first:10){ nodes { name target bypassActors(first:20){
    nodes { repositoryRoleDatabaseId repositoryRoleName bypassMode } } } } } }'
```

应输出 `admin(id=5)/ALWAYS`。**改动这条规则后请重新核对一次**：写错的后果是
保护静默失效，而界面上看起来一切正常。

> 用角色而不是具体用户，是为了以后增减管理员时不用改规则。

## 为什么现在不用操心「签名自动化」

2026-10-14 起发布包不做离线私钥签名，改成核对发布页给的 sha256 与文件大小（CI 会把
哈希自动印进发布说明）。于是「私钥要不要进 CI」这个长期争论在这条链路上不再存在，
也再没有「维护者必须先在本机签一次、用户才装得上」的窗口期。当时为什么不把私钥放进
CI（能合并 PR 的攻击者可以改 workflow 把密钥外传）、现在的信任边界、以及老版本怎么
过渡，都在 [release-signing.md](release-signing.md)。

## 如果网络不稳

下载产物时若**体积小于 GitHub 报告的大小**，是下载被截断（本机代理不稳），
不是产物被动过 —— 用 `curl -C -` 续传后重新比对 sha256 即可。一键更新遇到这种情况
会当场拒绝（「发布包大小不符，已拒绝安装」），再点一次更新就会重下，用户侧不需要
为此去查发布流程。
