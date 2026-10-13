// WorkBuddy Manager 本机控制台（Windows 原生部署）
//
// 双击即开一个新终端，提供部署 / 关闭 / 重启 / 状态四项操作。
// 项目根不写死：优先 WB_MANAGER_ROOT 环境变量，否则从 exe 所在目录向上查找
// 含 server/main.py 的目录，因此把 exe 放进项目里即可直接运行。
package main

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	managerPort  = 7864
	upstreamPort = 7863

	// Windows 进程创建标志
	createNewProcessGroup = 0x00000200
	// 子进程不分配控制台窗口。宿主本身没有控制台时（-H windowsgui 的静默版），
	// cmd.exe / python.exe 这类控制台程序会凭空弹出一个黑窗，必须显式压掉；
	// 它同时让子进程不再继承本终端，关掉本窗口不会把它一起带走。
	// 不可与 DETACHED_PROCESS(0x08) 并用：那样本标志会被忽略，
	// 子进程反而会新开一个可见控制台。
	createNoWindow = 0x08000000
)

// 由构建参数注入：控制台版走菜单，静默版直接部署并启动
var defaultAction = "menu"

var (
	kernel32               = syscall.NewLazyDLL("kernel32.dll")
	procSetConsoleOutputCP = kernel32.NewProc("SetConsoleOutputCP")
	procSetConsoleCP       = kernel32.NewProc("SetConsoleCP")
)

type layout struct {
	root          string
	upstream      string
	data          string
	venv          string
	python        string
	envFile       string
	managerPid    string
	managerOut    string
	managerErr    string
	upstreamStart string
	upstreamStop  string
	upstreamExe   string
	upstreamConf  string
	staticIndex   string
}

func main() {
	initConsole()

	l, err := resolveLayout()
	if err != nil {
		fail(err.Error())
		return
	}

	action := defaultAction
	if len(os.Args) > 1 {
		action = os.Args[1]
	}
	if action == "menu" {
		l.menu()
		return
	}
	if err := l.run(action); err != nil {
		fail(err.Error())
	}
}

func initConsole() {
	// 控制台默认代码页是 GBK，Go 输出的是 UTF-8，不改会显示乱码
	procSetConsoleOutputCP.Call(65001)
	procSetConsoleCP.Call(65001)
}

func resolveLayout() (*layout, error) {
	root := strings.TrimSpace(os.Getenv("WB_MANAGER_ROOT"))
	if root == "" {
		root = searchRoot()
	}
	if root == "" {
		return nil, fmt.Errorf("找不到 WorkBuddy Manager 项目目录：请把本程序放进项目目录，或设置 WB_MANAGER_ROOT 环境变量")
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(filepath.Join(root, "server", "main.py")); err != nil {
		return nil, fmt.Errorf("目录不像 WorkBuddy Manager 项目根（缺 server/main.py）：%s", root)
	}

	join := func(parts ...string) string {
		return filepath.Join(append([]string{root}, parts...)...)
	}
	return &layout{
		root:          root,
		upstream:      join("upstream"),
		data:          join("data"),
		venv:          join(".venv"),
		python:        join(".venv", "Scripts", "python.exe"),
		envFile:       join(".env"),
		managerPid:    join("data", "manager.pid"),
		managerOut:    join("data", "manager.out.log"),
		managerErr:    join("data", "manager.err.log"),
		upstreamStart: join("upstream", "start-workbuddy2api.cmd"),
		upstreamStop:  join("upstream", "stop-workbuddy2api.cmd"),
		upstreamExe:   join("upstream", "wb2api.exe"),
		upstreamConf:  join("upstream", "config.json"),
		staticIndex:   join("web", "out", "index.html"),
	}, nil
}

// searchRoot 从 exe 所在目录向上找项目根，兼容“exe 放在项目里”的用法
func searchRoot() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	dir := filepath.Dir(exe)
	for i := 0; i < 4; i++ {
		if _, err := os.Stat(filepath.Join(dir, "server", "main.py")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return ""
}

func (l *layout) menu() {
	in := bufio.NewReader(os.Stdin)
	for {
		fmt.Println()
		title("WorkBuddy Manager 本机控制台")
		fmt.Println("1. 部署并启动")
		fmt.Println("2. 关闭管理端和上游")
		fmt.Println("3. 重启管理端和上游")
		fmt.Println("4. 查看状态")
		fmt.Println("0. 退出")
		fmt.Println()
		fmt.Printf("请输入选项：")
		line, err := in.ReadString('\n')
		if err != nil {
			return
		}
		choice := strings.TrimSpace(line)
		if choice == "0" {
			return
		}
		if err := l.run(choiceToAction(choice)); err != nil {
			fail(err.Error())
		}
		fmt.Println()
		fmt.Print("按回车返回菜单...")
		if _, err := in.ReadString('\n'); err != nil {
			return
		}
	}
}

func choiceToAction(choice string) string {
	switch choice {
	case "1":
		return "deploy"
	case "2":
		return "stop"
	case "3":
		return "restart"
	case "4":
		return "status"
	default:
		return ""
	}
}

func (l *layout) run(action string) error {
	switch action {
	case "deploy":
		return l.deploy()
	case "stop":
		return l.stop()
	case "restart":
		return l.restart()
	case "status":
		l.status()
		return nil
	default:
		return fmt.Errorf("未知操作：%q（可用：deploy / stop / restart / status）", action)
	}
}

func (l *layout) deploy() error {
	if err := l.ensure(); err != nil {
		return err
	}
	if err := l.startUpstream(); err != nil {
		return err
	}
	if err := l.startManager(); err != nil {
		return err
	}
	fmt.Println()
	ok("部署完成。浏览器打开 http://127.0.0.1:%d", managerPort)
	return nil
}

func (l *layout) stop() error {
	l.stopManager()
	l.stopUpstream()
	return nil
}

func (l *layout) restart() error {
	l.stopManager()
	l.stopUpstream()
	if err := l.ensure(); err != nil {
		return err
	}
	if err := l.startUpstream(); err != nil {
		return err
	}
	if err := l.startManager(); err != nil {
		return err
	}
	ok("重启完成。")
	return nil
}

func (l *layout) status() {
	title("WorkBuddy 当前状态")

	if pid, running := l.managerPID(); running {
		ok("管理端：运行中，PID=%d，http://127.0.0.1:%d", pid, managerPort)
	} else if portOpen(managerPort) {
		warn("管理端：%d 端口被占用，但不是本控制台启动的进程", managerPort)
	} else {
		quiet("管理端：未运行")
	}

	if portOpen(upstreamPort) {
		ok("上游：运行中，http://127.0.0.1:%d", upstreamPort)
	} else {
		quiet("上游：未运行")
	}

	if portOpen(managerPort) {
		url := fmt.Sprintf("http://127.0.0.1:%d/api/healthz", managerPort)
		client := &http.Client{Timeout: 5 * time.Second}
		resp, err := client.Get(url)
		if err != nil {
			warn("管理端健康检查失败：%v", err)
			return
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		ok("管理端健康检查：%d %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
}

// ensure 只补缺失的部分：依赖与上游二进制存在时不会重复安装或编译
func (l *layout) ensure() error {
	title("检查部署环境")

	required := []string{l.envFile, l.upstreamConf, l.upstreamStart, l.upstreamStop}
	for _, path := range required {
		if _, err := os.Stat(path); err != nil {
			return fmt.Errorf("缺少必需文件：%s", path)
		}
	}
	for _, dir := range []string{l.data, filepath.Join(l.upstream, "auths"), filepath.Join(l.upstream, "data")} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("创建目录失败 %s：%w", dir, err)
		}
	}
	if _, err := os.Stat(l.staticIndex); err != nil {
		return fmt.Errorf("缺少前端构建产物：%s", l.staticIndex)
	}

	if _, err := os.Stat(l.python); err != nil {
		if err := l.createVenv(); err != nil {
			return err
		}
	}
	if err := l.runIfMissing([]string{l.python, "-c", "import fastapi, uvicorn, httpx, pydantic, psycopg, socksio"},
		"正在安装管理端依赖...",
		[]string{l.python, "-m", "pip", "install", "-r", filepath.Join(l.root, "server", "requirements.txt")}); err != nil {
		return err
	}

	if _, err := os.Stat(l.upstreamExe); err != nil {
		if err := l.buildUpstream(); err != nil {
			return err
		}
	}

	ok("部署文件检查通过。")
	return nil
}

func (l *layout) createVenv() error {
	py, err := exec.LookPath("py.exe")
	if err != nil {
		return fmt.Errorf("找不到 py.exe，无法创建 Python 虚拟环境")
	}
	fmt.Println("正在创建 Python 虚拟环境...")
	cmd := exec.Command(py, "-3.12", "-m", "venv", l.venv)
	cmd.Dir = l.root
	return runCommand(cmd, "创建 Python 虚拟环境失败")
}

func (l *layout) buildUpstream() error {
	goExe, err := exec.LookPath("go.exe")
	if err != nil {
		return fmt.Errorf("找不到 go.exe，无法构建上游服务")
	}
	fmt.Println("正在构建 workbuddy2api 上游服务...")
	cmd := exec.Command(goExe, "build", "-trimpath", "-ldflags=-s -w", "-o", "wb2api.exe", "./cmd/server")
	cmd.Dir = l.upstream
	return runCommand(cmd, "构建 workbuddy2api 失败")
}

// runIfMissing 先跑 probe，失败时再执行 fix
func (l *layout) runIfMissing(probe []string, notice string, fix []string) error {
	check := exec.Command(probe[0], probe[1:]...)
	check.Dir = l.root
	check.Stdout, check.Stderr = io.Discard, io.Discard
	check.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
	if check.Run() == nil {
		return nil
	}
	fmt.Println(notice)
	cmd := exec.Command(fix[0], fix[1:]...)
	cmd.Dir = l.root
	return runCommand(cmd, "安装管理端依赖失败")
}

func (l *layout) startUpstream() error {
	if portOpen(upstreamPort) {
		ok("上游已在 127.0.0.1:%d 运行。", upstreamPort)
		return nil
	}
	fmt.Println("正在启动 workbuddy2api 上游服务...")
	cmd := exec.Command("cmd.exe", "/d", "/c", l.upstreamStart)
	cmd.Dir = l.upstream
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
	if err := runCommand(cmd, "上游启动脚本失败"); err != nil {
		return err
	}
	if !waitPort(upstreamPort, true, 30) {
		return fmt.Errorf("上游未在 %d 端口监听，请查看 %s", upstreamPort, filepath.Join(l.upstream, "data", "server.err.log"))
	}
	ok("上游已启动：http://127.0.0.1:%d", upstreamPort)
	return nil
}

func (l *layout) stopUpstream() {
	if !portOpen(upstreamPort) {
		quiet("上游当前未运行。")
		return
	}
	fmt.Println("正在关闭 workbuddy2api 上游服务...")
	cmd := exec.Command("cmd.exe", "/d", "/c", l.upstreamStop)
	cmd.Dir = l.upstream
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
	if err := runCommand(cmd, "上游停止脚本失败"); err != nil {
		warn("%v", err)
	}
	if waitPort(upstreamPort, false, 15) {
		ok("上游已关闭。")
	} else {
		warn("上游端口仍在监听，请检查上游日志。")
	}
}

func (l *layout) startManager() error {
	if pid, running := l.managerPID(); running {
		ok("管理端已运行，PID=%d。", pid)
		return nil
	}
	fmt.Println("正在启动 WorkBuddy 管理端...")
	out, err := os.Create(l.managerOut)
	if err != nil {
		return fmt.Errorf("无法写入管理端日志：%w", err)
	}
	defer out.Close()
	errOut, err := os.Create(l.managerErr)
	if err != nil {
		return fmt.Errorf("无法写入管理端日志：%w", err)
	}
	defer errOut.Close()

	cmd := exec.Command(l.python, "-m", "uvicorn", "server.main:app",
		"--host", "127.0.0.1", "--port", strconv.Itoa(managerPort), "--env-file", ".env")
	cmd.Dir = l.root
	cmd.Stdout, cmd.Stderr = out, errOut
	// 不弹窗且不继承本终端：关掉这个窗口时管理端继续跑
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow | createNewProcessGroup}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("启动管理端失败：%w", err)
	}
	pid := cmd.Process.Pid
	_ = os.WriteFile(l.managerPid, []byte(strconv.Itoa(pid)), 0o644)
	cmd.Process.Release()

	if !waitPort(managerPort, true, 30) {
		return fmt.Errorf("管理端未在 %d 端口监听，请查看 %s", managerPort, l.managerErr)
	}
	ok("管理端已启动：http://127.0.0.1:%d", managerPort)
	return nil
}

func (l *layout) stopManager() {
	pid, running := l.managerPID()
	if !running {
		if portOpen(managerPort) {
			warn("管理端在 %d 端口上，但不是本控制台启动的进程，未做处理。", managerPort)
		} else {
			quiet("管理端当前未运行。")
		}
		_ = os.Remove(l.managerPid)
		return
	}
	fmt.Printf("正在关闭管理端 PID=%d...\n", pid)
	kill := exec.Command("taskkill", "/PID", strconv.Itoa(pid), "/T", "/F")
	kill.Stdout, kill.Stderr = io.Discard, io.Discard
	_ = kill.Run()
	_ = os.Remove(l.managerPid)
	if waitPort(managerPort, false, 15) {
		ok("管理端已关闭。")
	} else {
		warn("管理端端口仍在监听。")
	}
}

// managerPID 以「PID 文件 + 端口在听」双重确认为准，避免误杀别人的进程
func (l *layout) managerPID() (int, bool) {
	raw, err := os.ReadFile(l.managerPid)
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return 0, false
	}
	if !portOpen(managerPort) {
		_ = os.Remove(l.managerPid) // 陈旧记录，清掉
		return 0, false
	}
	return pid, true
}

func portOpen(port int) bool {
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), time.Second)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

func waitPort(port int, wantOpen bool, seconds int) bool {
	for i := 0; i < seconds; i++ {
		if portOpen(port) == wantOpen {
			return true
		}
		time.Sleep(time.Second)
	}
	return false
}

func runCommand(cmd *exec.Cmd, failText string) error {
	cmd.Stdout, cmd.Stderr, cmd.Stdin = os.Stdout, os.Stderr, os.Stdin
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s：%v", failText, err)
	}
	return nil
}

func title(text string) {
	fmt.Printf("\n%s\n%s\n%s\n", strings.Repeat("=", 64), text, strings.Repeat("=", 64))
}

func ok(format string, args ...any)    { fmt.Printf("[完成] "+format+"\n", args...) }
func warn(format string, args ...any)  { fmt.Printf("[注意] "+format+"\n", args...) }
func quiet(format string, args ...any) { fmt.Printf("       "+format+"\n", args...) }

func fail(message string) {
	warn("%s", message)
	fmt.Println()
	fmt.Print("按回车退出...")
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	os.Exit(1)
}
