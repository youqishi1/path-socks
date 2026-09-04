package main

import (
	"bufio"
	"context"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const testUUID = "00000000-0000-0000-0000-000000000001"

func TestGateway32Concurrent(t *testing.T) {
	users := filepath.Join(t.TempDir(), "users.db")
	if err := os.WriteFile(users, []byte("test|"+testUUID+"\n"), 0600); err != nil {
		t.Fatal(err)
	}

	echo := listenTCP(t)
	go acceptEcho(echo)
	socks := listenTCP(t)
	go acceptSocks(socks)

	originalResolver := resolveProxyIP
	resolveProxyIP = func(_ context.Context, host string) (net.IP, error) { return net.ParseIP(host), nil }
	defer func() { resolveProxyIP = originalResolver }()

	app := &gateway{usersFile: users, upgrader: websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}}
	server := httptest.NewServer(http.HandlerFunc(app.handle))
	defer server.Close()

	socksPort := socks.Addr().(*net.TCPAddr).Port
	wsURL := "ws" + server.URL[4:] + "/proxyip=socks5://127.0.0.1:" + fmt.Sprint(socksPort)
	target := echo.Addr().(*net.TCPAddr)
	errors := make(chan error, 32)
	for i := 0; i < 32; i++ {
		go func() { errors <- gatewayRoundTrip(wsURL, target.Port) }()
	}
	for i := 0; i < 32; i++ {
		if err := <-errors; err != nil {
			t.Fatal(err)
		}
	}
}

func gatewayRoundTrip(wsURL string, targetPort int) error {
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		return err
	}
	defer ws.Close()
	_ = ws.SetReadDeadline(time.Now().Add(5 * time.Second))
	uuid, _ := hex.DecodeString("00000000000000000000000000000001")
	header := append([]byte{0}, uuid...)
	header = append(header, 0, 1, byte(targetPort>>8), byte(targetPort), 1, 127, 0, 0, 1)
	header = append(header, []byte("hello")...)
	if err = ws.WriteMessage(websocket.BinaryMessage, header); err != nil {
		return err
	}
	_, response, err := ws.ReadMessage()
	if err != nil {
		return err
	}
	if string(response) != "\x00\x00hello" {
		return fmt.Errorf("unexpected response %q", response)
	}
	return nil
}

func TestPrivateProxyBlocked(t *testing.T) {
	for _, value := range []string{"127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1"} {
		if isPublicIP(net.ParseIP(value)) {
			t.Fatalf("private address accepted: %s", value)
		}
	}
}

func TestParseSocks5(t *testing.T) {
	got, err := parseSocks5("socks5://user:pass@example.com:1080")
	if err != nil || got.host != "example.com" || got.port != 1080 || got.username != "user" || got.password != "pass" {
		t.Fatalf("unexpected parse result: %#v %v", got, err)
	}
	if _, err = parseSocks5("http://example.com:8080"); err == nil {
		t.Fatal("HTTP proxy was accepted")
	}
}

func listenTCP(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	return listener
}

func acceptEcho(listener net.Listener) {
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		go func() { defer conn.Close(); _, _ = io.Copy(conn, conn) }()
	}
}

func acceptSocks(listener net.Listener) {
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		go handleTestSocks(conn)
	}
}

func handleTestSocks(client net.Conn) {
	defer client.Close()
	reader := bufio.NewReader(client)
	head := make([]byte, 2)
	if _, err := io.ReadFull(reader, head); err != nil {
		return
	}
	methods := make([]byte, int(head[1]))
	if _, err := io.ReadFull(reader, methods); err != nil {
		return
	}
	_, _ = client.Write([]byte{5, 0})
	request := make([]byte, 4)
	if _, err := io.ReadFull(reader, request); err != nil {
		return
	}
	var host string
	switch request[3] {
	case 1:
		ip := make([]byte, 4)
		_, _ = io.ReadFull(reader, ip)
		host = net.IP(ip).String()
	case 3:
		length, _ := reader.ReadByte()
		name := make([]byte, int(length))
		_, _ = io.ReadFull(reader, name)
		host = string(name)
	default:
		return
	}
	portBytes := make([]byte, 2)
	_, _ = io.ReadFull(reader, portBytes)
	port := int(portBytes[0])<<8 | int(portBytes[1])
	upstream, err := net.Dial("tcp", net.JoinHostPort(host, fmt.Sprint(port)))
	if err != nil {
		return
	}
	defer upstream.Close()
	_, _ = client.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0})
	go io.Copy(upstream, reader)
	_, _ = io.Copy(client, upstream)
}
