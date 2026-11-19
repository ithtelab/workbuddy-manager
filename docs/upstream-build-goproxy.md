# 上游容器构建的 GOPROXY 兜底（国内网络更新卡死）

## 现象

国内网络（无法直连 proxy.golang.org）的面板“全部更新”停在“同步随包上游”的
重建步骤，更新日志尾部形如：

```text
#13 [build 4/6] RUN go mod download
#13 60.21 go: github.com/cespare/xxhash/v2@v2.3.0: Get "https://proxy.golang.org/...": dial tcp ...:443: i/o timeout
#13 ERROR: process "/bin/sh -c go mod download" did not complete successfully: exit code: 1
重建失败（exit 1）
原因判断：网络问题（拉取基础镜像或 Go 依赖失败），稍后重试
```

旧容器不受影响、服务继续可用，但面板、随包上游停在旧版本，重试永远卡在同一处。

## 根因

上游 `Dockerfile` 的构建阶段没有设置 `GOPROXY`，`go mod download` 走 Go 默认的
`proxy.golang.org`，国内网络不可达即失败。基础镜像有腾讯云等 registry mirror
可用，卡点只在 Go 模块代理。

更新器每次同步随包上游都会用包内 `Dockerfile` 覆盖本地（只保留
`docker-compose.yml` 的定制），所以在部署机上手改 `Dockerfile` 加代理只能撑到
下一次更新，问题必然复发。

## 修法（0020）

`Dockerfile` 构建阶段显式设置默认代理，并保留构建参数覆盖口：

```dockerfile
ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY}
```

- 默认走 goproxy.cn（公共 CDN，海外同样可达，回源走 direct），不引入账号或
  私有依赖。
- 需要别的代理时构建可覆盖：
  `docker compose build --build-arg GOPROXY=https://proxy.golang.org,direct`。
- 只影响构建阶段的模块下载，运行期行为不变。

现场验证（腾讯云国内机型，2026-10-23）：同一台机器裸构建卡在 `go mod download`
60 秒超时。打上本补丁后 `docker compose build --no-cache` 全程完成，产物二进制
与代理前构建逐字节一致（md5 相同），确认代理只改变下载路径、不影响构建结果。
