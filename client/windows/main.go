// AW Remote Agent — Windows/Linux client
// Connects to the AW Remote Agent server via WebSocket.
// Executes commands locally and streams results back.
//
// Build (Windows):
//   GOOS=windows GOARCH=amd64 go build -o aw-remote-agent.exe .
// Build (Linux):
//   go build -o aw-remote-agent .
//
// Run:
//   aw-remote-agent.exe --server ws://YOUR_AW_SERVER:10011 --profile <uuid>
//   aw-remote-agent.exe --uninstall
package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	regKeyPath   = `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
	regValueName = "AWRemoteAgent"

	version = "1.4.0"

	pingInterval   = 20 * time.Second // how often client sends pings
	pongDeadline   = 10 * time.Second // how long to wait for pong before declaring dead
	writeTimeout   = 15 * time.Second // per-write deadline
	maxBackoff     = 2 * time.Minute  // cap on reconnect wait
	backoffBase    = 5 * time.Second
)

const (
	updateCheckInterval = 5 * time.Minute
	updateCheckTimeout  = 30 * time.Second
)

type UpdateInfo struct {
	Version string `json:"version"`
	SHA256  string `json:"sha256"`
	MinVer  string `json:"min_version,omitempty"`
}

// wsToHTTP converts ws://host:port or wss://host:port to http(s)://host:port
func wsToHTTP(wsURL string) string {
	if strings.HasPrefix(wsURL, "wss://") {
		return "https://" + strings.TrimPrefix(wsURL, "wss://")
	}
	return "http://" + strings.TrimPrefix(wsURL, "ws://")
}

// httpBase strips any path from the URL, returning scheme://host:port
func httpBase(rawURL string) string {
	// Find the third slash (after scheme://)
	s := rawURL
	slashes := 0
	for i, c := range s {
		if c == '/' {
			slashes++
			if slashes == 3 {
				return s[:i]
			}
		}
	}
	return s
}

func checkForUpdate(baseURL string) (*UpdateInfo, error) {
	url := baseURL + "/api/update/latest"
	client := &http.Client{Timeout: updateCheckTimeout}
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 404 {
		return nil, nil // no update available
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("server returned %d", resp.StatusCode)
	}
	var info UpdateInfo
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		return nil, err
	}
	return &info, nil
}

func downloadExe(baseURL, destPath string) error {
	url := baseURL + "/api/update/exe"
	client := &http.Client{Timeout: 5 * time.Minute}
	resp, err := client.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	f, err := os.OpenFile(destPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0755)
	if err != nil {
		return err
	}
	_, err = io.Copy(f, resp.Body)
	f.Close()
	return err
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func applyUpdate(newExe, currentExe string, launchArgs []string) {
	log.Printf("applying update: %s → %s, then restarting", newExe, currentExe)
	if runtime.GOOS == "windows" {
		argsStr := ""
		for _, a := range launchArgs {
			argsStr += `"` + a + `" `
		}
		script := fmt.Sprintf("@echo off\r\nping -n 4 localhost > nul\r\ncopy /y %q %q\r\ndel %q\r\nstart \"\" %q %s\r\n",
			newExe, currentExe, newExe, currentExe, argsStr)
		tmpBat := filepath.Join(os.TempDir(), "aw-update.cmd")
		os.WriteFile(tmpBat, []byte(script), 0644)
		exec.Command("cmd", "/c", "start", "/b", "", tmpBat).Start()
	} else {
		script := fmt.Sprintf("#!/bin/sh\nsleep 2\ncp -f %q %q\nexec %q %s\n",
			newExe, currentExe, currentExe, strings.Join(launchArgs, " "))
		tmpSh := filepath.Join(os.TempDir(), "aw-update.sh")
		os.WriteFile(tmpSh, []byte(script), 0755)
		exec.Command("sh", tmpSh).Start()
	}
	os.Exit(0)
}

// runSelfCheck hits the server's selfcheck endpoint and exits 0 on success, 1 on failure.
// Used by --test-mode to verify a newly downloaded binary can reach the server.
func runSelfCheck(serverURL string) {
	baseURL := httpBase(wsToHTTP(serverURL))
	url := baseURL + "/api/update/selfcheck"
	client := &http.Client{Timeout: 20 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		log.Printf("selfcheck FAIL: %v", err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.Printf("selfcheck FAIL: HTTP %d", resp.StatusCode)
		os.Exit(1)
	}
	log.Printf("selfcheck OK (HTTP 200 from %s)", url)
	os.Exit(0)
}

func updateLoop(serverURL string, launchArgs []string) {
	baseURL := httpBase(wsToHTTP(serverURL))
	ticker := time.NewTicker(updateCheckInterval)
	defer ticker.Stop()
	for range ticker.C {
		info, err := checkForUpdate(baseURL)
		if err != nil {
			log.Printf("update check failed: %v", err)
			continue
		}
		if info == nil {
			continue // no update
		}
		if info.Version == version {
			log.Printf("update check: already up to date (%s)", version)
			continue
		}
		log.Printf("update available: %s → %s, downloading...", version, info.Version)

		exePath, _ := os.Executable()
		newExe := exePath + ".new"
		if err := downloadExe(baseURL, newExe); err != nil {
			log.Printf("update download failed: %v", err)
			continue
		}

		if info.SHA256 != "" {
			got, err := sha256File(newExe)
			if err != nil || got != info.SHA256 {
				log.Printf("update sha256 mismatch (got %s, want %s), aborting", got, info.SHA256)
				os.Remove(newExe)
				continue
			}
		}

		// Run the new binary in --test-mode before applying.
		// It hits /api/update/selfcheck and exits 0 on success.
		log.Printf("running test-mode on new exe...")
		testCmd := exec.Command(newExe, "--test-mode", "--server", serverURL)
		testCmd.Stdout = os.Stderr
		testCmd.Stderr = os.Stderr
		if err := testCmd.Start(); err != nil {
			log.Printf("update test-mode start failed: %v, aborting", err)
			os.Remove(newExe)
			continue
		}
		testDone := make(chan error, 1)
		go func() { testDone <- testCmd.Wait() }()
		select {
		case testErr := <-testDone:
			if testErr != nil {
				log.Printf("update test-mode failed: %v, aborting", testErr)
				os.Remove(newExe)
				continue
			}
		case <-time.After(30 * time.Second):
			testCmd.Process.Kill()
			log.Printf("update test-mode timed out, aborting")
			os.Remove(newExe)
			continue
		}

		log.Printf("test-mode passed, applying update...")
		applyUpdate(newExe, exePath, launchArgs)
	}
}

// ── Auto-start (Windows registry) ────────────────────────────────────────────

func installAutoStart(exePath, serverURL, clientID string) error {
	idFlag := "--id"
	if strings.Contains(clientID, "-") && len(clientID) == 36 {
		idFlag = "--profile"
	}
	value := fmt.Sprintf(`"%s" --server %s %s %s`, exePath, serverURL, idFlag, clientID)
	return exec.Command("reg", "add", regKeyPath, "/v", regValueName, "/d", value, "/f").Run()
}

func uninstallAutoStart() error {
	return exec.Command("reg", "delete", regKeyPath, "/v", regValueName, "/f").Run()
}

// ── Message types ─────────────────────────────────────────────────────────────

type Message struct {
	Type     string      `json:"type"`
	ReqID    string      `json:"req_id,omitempty"`
	Command  string      `json:"command,omitempty"`
	Timeout  int         `json:"timeout,omitempty"`
	Info     *ClientInfo `json:"info,omitempty"`
	Stdout   string      `json:"stdout,omitempty"`
	Stderr   string      `json:"stderr,omitempty"`
	// ExitCode maps to the wire key "returncode" — the server's exec_response
	// and exec_done handlers both read msg["returncode"]; a mismatched key
	// here silently makes every reported exit code 0.
	ExitCode int    `json:"returncode,omitempty"`
	Stream   string `json:"stream,omitempty"`
	Error    string `json:"error,omitempty"`
	// FS fields
	FsOp     string `json:"op,omitempty"`
	FsPath   string `json:"path,omitempty"`
	FsData   string `json:"data,omitempty"`
	FsOffset int64  `json:"offset,omitempty"`
	FsSize   int    `json:"size,omitempty"`
	FsDest   string `json:"dest,omitempty"`
}

type FsEntry struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
	Mode  uint32 `json:"mode"`
	Mtime int64  `json:"mtime"`
}

type FsResponse struct {
	Type    string    `json:"type"`
	ReqID   string    `json:"req_id"`
	Entries []FsEntry `json:"entries,omitempty"`
	Stat    *FsEntry  `json:"stat,omitempty"`
	Data    string    `json:"data,omitempty"`
	Error   string    `json:"error,omitempty"`
}

// ── Thread-safe WebSocket writer ──────────────────────────────────────────────

type safeConn struct {
	mu   sync.Mutex
	conn *websocket.Conn
}

func (s *safeConn) writeJSON(v interface{}) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.conn.SetWriteDeadline(time.Now().Add(writeTimeout))
	return s.conn.WriteMessage(websocket.TextMessage, data)
}

func (s *safeConn) writePing() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.conn.SetWriteDeadline(time.Now().Add(writeTimeout))
	return s.conn.WriteMessage(websocket.PingMessage, nil)
}

func (s *safeConn) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.conn.Close()
}

// ── FS operations ─────────────────────────────────────────────────────────────

func infoToEntry(info os.FileInfo) FsEntry {
	return FsEntry{
		Name:  info.Name(),
		IsDir: info.IsDir(),
		Size:  info.Size(),
		Mode:  uint32(info.Mode()),
		Mtime: info.ModTime().Unix(),
	}
}

func handleFsOp(msg Message) FsResponse {
	resp := FsResponse{Type: "fs_response", ReqID: msg.ReqID}

	// Enforce --dir restriction: reject paths outside the allowed directory
	for _, p := range []string{msg.FsPath, msg.FsDest} {
		if p != "" && !isPathAllowed(p) {
			resp.Error = fmt.Sprintf("access denied: %q is outside allowed dir %q", p, allowedDir)
			return resp
		}
	}

	switch msg.FsOp {
	case "readdir":
		entries, err := os.ReadDir(msg.FsPath)
		if err != nil {
			resp.Error = err.Error()
			return resp
		}
		for _, e := range entries {
			info, err := e.Info()
			if err != nil {
				continue
			}
			resp.Entries = append(resp.Entries, infoToEntry(info))
		}

	case "stat":
		info, err := os.Stat(msg.FsPath)
		if err != nil {
			resp.Error = err.Error()
			return resp
		}
		e := infoToEntry(info)
		resp.Stat = &e

	case "read":
		size := msg.FsSize
		if size <= 0 {
			size = 65536
		}
		f, err := os.Open(msg.FsPath)
		if err != nil {
			resp.Error = err.Error()
			return resp
		}
		defer f.Close()
		buf := make([]byte, size)
		n, err := f.ReadAt(buf, msg.FsOffset)
		if err != nil && err != io.EOF {
			resp.Error = err.Error()
			return resp
		}
		resp.Data = base64.StdEncoding.EncodeToString(buf[:n])

	case "write":
		data, err := base64.StdEncoding.DecodeString(msg.FsData)
		if err != nil {
			resp.Error = err.Error()
			return resp
		}
		f, err := os.OpenFile(msg.FsPath, os.O_WRONLY|os.O_CREATE, 0644)
		if err != nil {
			resp.Error = err.Error()
			return resp
		}
		defer f.Close()
		if _, err = f.WriteAt(data, msg.FsOffset); err != nil {
			resp.Error = err.Error()
		}

	case "mkdir":
		if err := os.MkdirAll(msg.FsPath, 0755); err != nil {
			resp.Error = err.Error()
		}

	case "unlink":
		if err := os.Remove(msg.FsPath); err != nil {
			resp.Error = err.Error()
		}

	case "rename":
		if err := os.Rename(msg.FsPath, msg.FsDest); err != nil {
			resp.Error = err.Error()
		}

	case "truncate":
		if err := os.Truncate(msg.FsPath, msg.FsOffset); err != nil {
			resp.Error = err.Error()
		}

	default:
		resp.Error = "unknown op: " + msg.FsOp
	}

	return resp
}

// ── System info ───────────────────────────────────────────────────────────────

type ClientInfo struct {
	OS        string `json:"os"`
	Arch      string `json:"arch"`
	Hostname  string `json:"hostname"`
	Username  string `json:"username"`
	Version   string `json:"version"`
	CPUs      int    `json:"cpus"`
	RAMBytes  int64  `json:"ram_bytes"`
	OSVersion string `json:"os_version"`
	RootDir   string `json:"root_dir,omitempty"` // empty = unrestricted
}

// allowedDir is set once at startup via --dir flag. Empty = no restriction.
var allowedDir string

// isPathAllowed returns true if path is inside the allowed directory (or no restriction).
func isPathAllowed(path string) bool {
	if allowedDir == "" {
		return true
	}
	clean := filepath.Clean(path)
	root := filepath.Clean(allowedDir)
	if runtime.GOOS == "windows" {
		cu := strings.ToUpper(clean)
		ru := strings.ToUpper(root)
		return cu == ru || strings.HasPrefix(cu, ru+string(os.PathSeparator))
	}
	return clean == root || strings.HasPrefix(clean, root+string(os.PathSeparator))
}

func psValue(cmd string) string {
	out, err := exec.Command("powershell", "-NoProfile", "-Command", cmd).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func getInfo() *ClientInfo {
	hostname, _ := os.Hostname()
	username := os.Getenv("USERNAME")
	if username == "" {
		username = os.Getenv("USER")
	}
	info := &ClientInfo{
		OS:       runtime.GOOS,
		Arch:     runtime.GOARCH,
		Hostname: hostname,
		Username: username,
		Version:  version,
		CPUs:     runtime.NumCPU(),
		RootDir:  allowedDir,
	}
	if runtime.GOOS == "windows" {
		ramStr := psValue("(Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize")
		if kb, err := strconv.ParseInt(ramStr, 10, 64); err == nil {
			info.RAMBytes = kb * 1024
		}
		info.OSVersion = psValue("(Get-CimInstance Win32_OperatingSystem).Caption")
	}
	return info
}

// ── Command execution ─────────────────────────────────────────────────────────

// wsStreamWriter forwards each Write() as an exec_chunk message, so the
// server sees output as the process produces it instead of only after it
// exits — matches the Linux client's streaming behavior.
type wsStreamWriter struct {
	conn   *safeConn
	reqID  string
	stream string
}

func (w *wsStreamWriter) Write(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	if err := w.conn.writeJSON(Message{
		Type:   "exec_chunk",
		ReqID:  w.reqID,
		Stream: w.stream,
		Stdout: "", // unused for chunks; payload goes in FsData below
	}); false {
		_ = err // placeholder, replaced below
	}
	return len(p), nil
}

// runCommand streams stdout/stderr to the caller live via sc and returns
// only the final exit code — the output itself was already sent as chunks.
func runCommand(sc *safeConn, reqID, command string, timeout int) (exitCode int) {
	if timeout <= 0 {
		timeout = 30
	}
	var cmd *exec.Cmd
	if runtime.GOOS == "windows" {
		cmd = exec.Command("powershell", "-NoProfile", "-Command", command)
	} else {
		cmd = exec.Command("sh", "-c", command)
	}

	cmd.Stdout = &wsChunkWriter{conn: sc, reqID: reqID, stream: "stdout"}
	cmd.Stderr = &wsChunkWriter{conn: sc, reqID: reqID, stream: "stderr"}

	done := make(chan error, 1)
	if err := cmd.Start(); err != nil {
		sc.writeJSON(Message{Type: "exec_chunk", ReqID: reqID, Stream: "stderr", FsData: err.Error()})
		return 1
	}
	go func() { done <- cmd.Wait() }()

	select {
	case err := <-done:
		if err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				exitCode = exitErr.ExitCode()
			} else {
				exitCode = 1
				sc.writeJSON(Message{Type: "exec_chunk", ReqID: reqID, Stream: "stderr", FsData: err.Error()})
			}
		}
	case <-time.After(time.Duration(timeout) * time.Second):
		cmd.Process.Kill()
		sc.writeJSON(Message{
			Type: "exec_chunk", ReqID: reqID, Stream: "stderr",
			FsData: fmt.Sprintf("command timed out after %ds", timeout),
		})
		exitCode = 124
	}
	return
}

// wsChunkWriter is the io.Writer used as cmd.Stdout/cmd.Stderr — each Write
// call (as the OS pipe delivers data) becomes one exec_chunk message. Reuses
// the Message.FsData field for the payload (wire key "data") since it's
// already part of the shared Message struct.
type wsChunkWriter struct {
	conn   *safeConn
	reqID  string
	stream string
}

func (w *wsChunkWriter) Write(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	if err := w.conn.writeJSON(Message{
		Type:   "exec_chunk",
		ReqID:  w.reqID,
		Stream: w.stream,
		FsData: string(p),
	}); err != nil {
		return 0, err
	}
	return len(p), nil
}

// ── Connection ────────────────────────────────────────────────────────────────

// connect dials the server, handles the message loop, and returns when the
// connection is lost. The caller is responsible for reconnecting.
func connect(serverURL, clientID string) {
	url := fmt.Sprintf("%s/ws/client/%s", serverURL, clientID)
	log.Printf("connecting to %s", url)

	rawConn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		log.Printf("dial error: %v", err)
		return
	}
	sc := &safeConn{conn: rawConn}
	defer sc.close()

	connectedAt := time.Now()
	log.Printf("connected (profile=%s)", clientID)

	// Pong handler — extend read deadline on each pong received
	rawConn.SetPongHandler(func(appData string) error {
		rawConn.SetReadDeadline(time.Now().Add(pingInterval + pongDeadline))
		return nil
	})

	// Initial read deadline — must receive something within first ping cycle
	rawConn.SetReadDeadline(time.Now().Add(pingInterval + pongDeadline))

	// Send handshake
	info := getInfo()
	if err := sc.writeJSON(Message{Type: "handshake", Info: info}); err != nil {
		log.Printf("handshake write error: %v", err)
		return
	}
	log.Printf("handshake sent (os=%s/%s host=%s user=%s cpus=%d)",
		info.OS, info.Arch, info.Hostname, info.Username, info.CPUs)

	// Background ping ticker
	stopPing := make(chan struct{})
	go func() {
		ticker := time.NewTicker(pingInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := sc.writePing(); err != nil {
					log.Printf("ping write error: %v", err)
					sc.close()
					return
				}
				log.Printf("ping sent")
			case <-stopPing:
				return
			}
		}
	}()
	defer close(stopPing)

	for {
		_, raw, err := rawConn.ReadMessage()
		if err != nil {
			uptime := time.Since(connectedAt).Round(time.Second)
			log.Printf("read error after %s uptime: %v", uptime, err)
			return
		}

		var msg Message
		if err := json.Unmarshal(raw, &msg); err != nil {
			log.Printf("unmarshal error: %v (raw=%s)", err, string(raw))
			continue
		}

		// Extend read deadline on any server activity
		rawConn.SetReadDeadline(time.Now().Add(pingInterval + pongDeadline))

		switch msg.Type {
		case "ping":
			if err := sc.writeJSON(Message{Type: "pong"}); err != nil {
				log.Printf("pong write error: %v", err)
				return
			}

		case "exec":
			log.Printf("exec req_id=%s cmd=%q", msg.ReqID, msg.Command)
			go func(reqID, command string, timeout int) {
				start := time.Now()
				stdout, stderr, exitCode := runCommand(command, timeout)
				elapsed := time.Since(start).Round(time.Millisecond)
				log.Printf("exec done req_id=%s exit=%d elapsed=%s", reqID, exitCode, elapsed)
				sc.writeJSON(Message{
					Type:     "exec_response",
					ReqID:    reqID,
					Stdout:   stdout,
					Stderr:   stderr,
					ExitCode: exitCode,
				})
			}(msg.ReqID, msg.Command, msg.Timeout)

		case "fs_request":
			log.Printf("fs req_id=%s op=%s path=%s", msg.ReqID, msg.FsOp, msg.FsPath)
			go func(m Message) {
				fsResp := handleFsOp(m)
				if fsResp.Error != "" {
					log.Printf("fs error req_id=%s op=%s: %s", m.ReqID, m.FsOp, fsResp.Error)
				}
				sc.writeJSON(fsResp)
			}(msg)

		default:
			log.Printf("unknown message type: %s", msg.Type)
		}
	}
}

// ── Logging setup ─────────────────────────────────────────────────────────────

func setupLogging(exePath string) {
	logDir := filepath.Dir(exePath)
	logPath := filepath.Join(logDir, "aw-remote-agent.log")
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		// Fall back to stderr only
		log.SetPrefix("[aw-remote-agent] ")
		log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
		return
	}
	// Write to both file and stderr
	mw := io.MultiWriter(os.Stderr, f)
	log.SetOutput(mw)
	log.SetPrefix("[aw-remote-agent] ")
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
	log.Printf("log file: %s", logPath)
}

// ── Main ──────────────────────────────────────────────────────────────────────

func main() {
	server    := flag.String("server", "ws://localhost:10011", "AW Remote Agent server WebSocket URL")
	id        := flag.String("id", "", "Client ID (default: <os>-<hostname>)")
	profile   := flag.String("profile", "", "Remote Agent profile UUID (overrides --id)")
	dir       := flag.String("dir", "", "Restrict filesystem access to this directory (default: unrestricted)")
	uninstall := flag.Bool("uninstall", false, "Remove from Windows auto-start and exit")
	testMode  := flag.Bool("test-mode", false, "Self-check: ping server and exit 0 on success")
	flag.Parse()

	allowedDir = *dir

	exePath, _ := os.Executable()
	setupLogging(exePath)

	log.Printf("starting version=%s os=%s/%s", version, runtime.GOOS, runtime.GOARCH)

	// In test-mode we only verify the binary can reach the server.
	if *testMode {
		runSelfCheck(*server)
		return // runSelfCheck calls os.Exit, but be explicit
	}

	if *uninstall {
		if err := uninstallAutoStart(); err != nil {
			log.Fatalf("uninstall failed: %v", err)
		}
		log.Println("removed from auto-start")
		os.Exit(0)
	}

	// --profile takes precedence over --id
	clientID := *profile
	if clientID == "" {
		clientID = *id
	}
	if clientID == "" {
		hostname, err := os.Hostname()
		if err != nil {
			hostname = "unknown"
		}
		clientID = strings.ToLower(runtime.GOOS) + "-" + hostname
	}

	if allowedDir != "" {
		log.Printf("filesystem restricted to: %s", allowedDir)
	}
	log.Printf("profile=%s server=%s", clientID, *server)

	// Register in Windows auto-start
	if runtime.GOOS == "windows" {
		if err := installAutoStart(exePath, *server, clientID); err != nil {
			log.Printf("auto-start registration failed: %v", err)
		} else {
			log.Printf("registered for auto-start (HKCU Run)")
		}
	}

	// Collect launch args for auto-update restarts
	launchArgs := []string{"--server", *server}
	if strings.Contains(clientID, "-") && len(clientID) == 36 {
		launchArgs = append(launchArgs, "--profile", clientID)
	} else {
		launchArgs = append(launchArgs, "--id", clientID)
	}
	if allowedDir != "" {
		launchArgs = append(launchArgs, "--dir", allowedDir)
	}
	go updateLoop(*server, launchArgs)

	// Reconnect loop with exponential backoff
	attempt := 0
	for {
		attempt++
		log.Printf("connect attempt #%d", attempt)
		connect(*server, clientID)

		backoff := time.Duration(math.Min(
			float64(backoffBase)*math.Pow(1.5, float64(attempt-1)),
			float64(maxBackoff),
		))
		log.Printf("disconnected — reconnecting in %s (attempt #%d)", backoff.Round(time.Second), attempt+1)
		time.Sleep(backoff)
	}
}
