# fork 自建镜像：把镜像推到你自己的 GHCR

> **先看清默认选择：直接用上游官方镜像。**
> 绝大多数情况不需要自建 —— `docker-compose.yml` 默认就拉上游官方的
> `ghcr.io/ithtelab/workbuddy-manager:latest`，什么都不用配。只有当你**确实要改代码
> 再自己用** —— 换默认配置、加依赖、或不想依赖别人的镜像仓库 —— 才需要下面这套
> 自建方案。本目录是进阶可选项，不是默认用法。

本目录的 `build-image.yml` 就是那份自建工作流，**上游不启用它**，只在 fork 里用。

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
docker pull ghcr.io/<你的用户名>/workbuddy-manager:latest
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

## 让 compose 用你的镜像

`docker-compose.yml` 默认拉上游官方镜像，不用改文件。跑完上面的自建工作流后，
在 compose 同目录的 `.env` 里加一行，就切到你自己的 GHCR：

```bash
# .env
WB_MANAGER_IMAGE=ghcr.io/<你的用户名>/workbuddy-manager:latest
```

`docker compose up -d` 重建即可。不设这一行时始终用上游
`ghcr.io/ithtelab/workbuddy-manager:latest`，所以 fork 仓库的 compose 文件不用改动，
上游更新也能直接合进来。

> 镜像名用的是 `workbuddy-manager`，和你自己的命名空间拼在一起就是
> `ghcr.io/<你的用户名>/workbuddy-manager`。注意 `<你的用户名>` 这一段不能省 ——
> 省略就变成上游官方镜像了。

## 如果推送报 write_package

历史上这个名字曾被一个**没链接到仓库**的同名包占用，导致 fork 的 GITHUB_TOKEN
写不进去，报 `denied: permission_denied: write_package`。这种「包存在但写不进去」
的状态，即使把包链接到仓库也未必能解。

真遇到了不用改包名：去 GHCR 网页把 `ghcr.io/<你的用户名>/workbuddy-manager`
这个旧包删掉，下次构建时 GITHUB_TOKEN 推送会自动建包并关联到本仓库，权限就干净了。
（`server/tests/test_docker_deploy.py` 里有一组约束盯着镜像名，别换成别的名字。）

## 可选：同时推 Docker Hub

加两个仓库 Secret 就会**自动启用**，不需要改工作流文件：

| Secret | 取值 |
|---|---|
| `DOCKERHUB_USERNAME` | 你的 Docker Hub 用户名 |
| `DOCKERHUB_TOKEN` | Docker Hub 的 Access Token（**不是**登录密码） |

Token 在 <https://hub.docker.com/settings/security> 生成，权限选 **Read & Write**。
两个 Secret 缺任一时，Docker Hub 那几步会自动跳过，**不会**把工作流标红 ——
「没配」是正常状态，不是失败。推上去的名字是
`docker.io/<DOCKERHUB_USERNAME>/workbuddy-manager`。

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
