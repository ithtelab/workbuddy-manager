# fork 自建镜像：把镜像推到你自己的 GHCR

上游那几份镜像是维护者发版时构建的。如果你要**改点什么再自己用** —— 换默认配置、
加个依赖、或者只是不想依赖别人的镜像仓库 —— 可以 fork 本仓库，让它用自己的
GitHub Actions 构建，推到你自己账号下的镜像仓库。

本目录的 `build-image.yml` 就是那份工作流，**上游不启用它**，只在 fork 里用。

## 怎么用

在 fork 里把工作流放到位，然后推一次代码即可：

```bash
# 1) 在 GitHub 上 fork 本仓库，然后拉到你本机
git clone https://github.com/<你的用户名>/workbuddy-manager.git
cd workbuddy-manager

# 2) 把 fork 专用工作流复制到 Actions 认的位置
mkdir -p .github/workflows
cp deploy/fork-image/build-image.yml .github/workflows/

# 3) 提交并推送（触发构建；也可到 Actions 页手动跑「Build Image」）
git add .github/workflows/build-image.yml
git commit -m "ci: 构建自己的镜像"
git push
```

构建大约几分钟（首次会久一些：amd64 与 arm64 两个架构都要出）。**工作流不用改任何
内容** —— 镜像推到谁名下、打哪些标签、镜像里记什么来源信息，都由它按你 fork 的实际
情况决定（见下一节）。

完成后拉取：

```bash
docker pull ghcr.io/<你的用户名>/workbuddy-manager-multiarch:latest
```

`<你的用户名>` 不用自己拼：那次运行页面的摘要里会直接列出可以原样粘贴的命令。

镜像同时提供 `linux/amd64` 与 `linux/arm64`，`docker pull` 会按你的机器架构自动选择。

## 它会自动适配你的仓库

`build-image.yml` 是**零配置**的，下面这些都在运行时从仓库信息推出来：

| 项 | 从哪里来 |
|---|---|
| 镜像归属 | 仓库所有者 —— 谁 fork 就推到谁名下；含大写字母时自动转小写（GHCR 不收大写镜像名） |
| 触发分支 | 你 fork 的**默认分支**，不写死 `main`；默认分支改了名也照常触发 |
| 镜像里的来源标签 | `org.opencontainers.image.source` / `revision` 指向你的 fork 与那次提交 |
| 推 Docker Hub | 有那两个 Secret 就推、没有就跳过，同样不用改文件 |

默认分支之外的推送会进入工作流但被**跳过**，不会覆盖 `latest`，也不花构建时间。

## 在 compose 里换成你自己的镜像

`docker-compose.yml` 默认是本地构建（`build:` 那段）。想直接用拉下来的镜像，
把 `build:` 整段删掉、把 `image:` 改成你的镜像名：

```yaml
services:
  workbuddy-manager:
    # 用你自己的镜像，不再本地构建
    image: ghcr.io/<你的用户名>/workbuddy-manager-multiarch:latest
```

> 镜像名比上游多一个 `-multiarch` 后缀，**不是笔误**。用上游那个
> `workbuddy-manager` 包名时，该名字在命名空间下可能已被一个**没有链接到本仓库**
> 的同名包占用 —— 那种包你的 fork 拿不到写权限，推送会直接失败。换个包名最省事，
> 顺带也避免和上游的镜像混在一起认不出来。

## 为什么镜像名多一个 -multiarch

同上。这一条被测试钉着（`server/tests/test_docker_deploy.py` 里专门有一组约束），
所以别顺手改回上游的包名 —— 改了在部分账号下会推送失败，而且失败信息
（`denied: permission_denied: write_package`）不容易联想到包名冲突。

## 可选：同时推 Docker Hub

加两个仓库 Secret 就会**自动启用**，不需要改工作流文件：

| Secret | 取值 |
|---|---|
| `DOCKERHUB_USERNAME` | 你的 Docker Hub 用户名 |
| `DOCKERHUB_TOKEN` | Docker Hub 的 Access Token（**不是**登录密码） |

Token 在 <https://hub.docker.com/settings/security> 生成，权限选 **Read & Write**。
两个 Secret 缺任一时，Docker Hub 那几步会自动跳过，**不会**把工作流标红 ——
「没配」是正常状态，不是失败。推上去的名字是
`docker.io/<DOCKERHUB_USERNAME>/workbuddy-manager-multiarch`。

## 它不做什么

**不创建 Release。** 发布包只由上游维护者正式发出；在 fork 上造一个 Release，
用户端的一键更新会照装（2026-10-13 起包只做摘要校验，不再有「不是维护者签的」这道
门槛）—— 也就是说那种包对你的用户同样有效，请自己确认来源可信。你自己的镜像不在那套
体系里：它的完整性依赖镜像仓库的
digest 与你 GitHub 账号的安全。

## 两点注意

- **镜像不会自动跟上游走**。你的 fork 要自己同步上游代码（GitHub 上的
  `Sync fork`，或把上游加为 remote 后 `git pull`），推上去才会重建镜像。
- **不想用 CI 也行**。本机装了 Docker 的话，`docker compose up -d --build` 就是
  同一条构建路径（`web/out` 不存在时会自动在容器内构建前端），只是出来的镜像
  只在你这台机器上，不会推到仓库。
