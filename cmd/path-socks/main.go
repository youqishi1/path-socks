package main

import (
	"bufio"
	"context"
	"crypto/subtle"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

const (
	connectTimeout   = 8 * time.Second
	handshakeTimeout = 8 * time.Second
	maxPathLength    = 4096
	maxInitialLength = 64 * 1024
	copyBufferSize   = 32 * 1024
)

type gateway struct {
	usersFile string
	upgrader  websocket.Upgrader
}

type socksConfig struct {
	host     string
	port     uint16
	username string
	password string
}

type vlessRequest struct {
	version byte
	command byte
	host    string
	port    uint16
	payload []byte
	uuid    []byte
}

func main() {
	listen := flag.String("listen", "127.0.0.1:18080", "HTTP listen address")
	users := flag.String("users", "/etc/path-socks/users.db", "user UUID database")
	portFile := flag.String("port-file", "", "public IPv4 TLS port file (requires tls-pem)")
	pemFile := flag.String("tls-pem", "", "combined certificate chain and private key PEM")
	check := flag.Bool("check", false, "validate configuration without opening a port")
	flag.Parse()
	if *portFile != "" {
		address, err := publicAddress(*portFile, *pemFile)
		if err != nil {
			log.Fatal(err)
		}
		*listen = address
	}
	var certificates atomic.Pointer[tls.Certificate]
	if *pemFile != "" {
		if err := refreshCertificate(*pemFile, &certificates); err != nil {
			log.Fatal(err)
		}
	}
	if _, err := os.ReadFile(*users); err != nil {
		log.Fatal(err)
	}
	if *check {
		return
	}

	app := &gateway{
		usersFile: *users,
		upgrader: websocket.Upgrader{
			ReadBufferSize:  copyBufferSize,
			WriteBufferSize: copyBufferSize,
			CheckOrigin:     func(*http.Request) bool { return true },
		},
	}

	server := &http.Server{
		Addr:              *listen,
		Handler:           http.HandlerFunc(app.serveHTTP),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    8 * 1024,
		ErrorLog:          log.New(io.Discard, "", 0),
	}
	if *pemFile != "" {
		server.TLSConfig = &tls.Config{
			MinVersion: tls.VersionTLS12,
			NextProtos: []string{"http/1.1"},
			GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
				return certificates.Load(), nil
			},
		}
		// Disable HTTP/2: the client protocol here is HTTP/1.1 WebSocket upgrade.
		server.TLSNextProto = make(map[string]func(*http.Server, *tls.Conn, http.Handler))
		go func() {
			ticker := time.NewTicker(time.Minute)
			defer ticker.Stop()
			for range ticker.C {
				if err := refreshCertificate(*pemFile, &certificates); err != nil {
					log.Print("certificate reload failed; retaining previous certificate")
				}
			}
		}()
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}()

	log.Printf("path-socks listening on %s", *listen)
	var err error
	if *pemFile != "" {
		err = server.ListenAndServeTLS("", "")
	} else {
		err = server.ListenAndServe()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func publicAddress(portFile, pemFile string) (string, error) {
	if pemFile == "" {
		return "", errors.New("public port requires a TLS certificate")
	}
	data, err := os.ReadFile(portFile)
	if err != nil {
		return "", err
	}
	port, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || port < 10240 || port > 65535 {
		return "", errors.New("public port must be between 10240 and 65535")
	}
	return net.JoinHostPort("0.0.0.0", strconv.Itoa(port)), nil
}

func refreshCertificate(path string, destination *atomic.Pointer[tls.Certificate]) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	certificate, err := tls.X509KeyPair(data, data)
	if err != nil {
		return err
	}
	if certificate.Leaf != nil && (time.Now().Before(certificate.Leaf.NotBefore) || time.Now().After(certificate.Leaf.NotAfter)) {
		return errors.New("TLS certificate is not currently valid")
	}
	destination.Store(&certificate)
	return nil
}

func (g *gateway) serveHTTP(w http.ResponseWriter, r *http.Request) {
	// ServeMux cleans double slashes, which would redirect a literal socks5:// Path.
	if r.URL.Path == "/health" {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "ok\n")
		return
	}
	g.handle(w, r)
}

func (g *gateway) handle(w http.ResponseWriter, r *http.Request) {
	if !websocket.IsWebSocketUpgrade(r) {
		http.NotFound(w, r)
		return
	}
	if len(r.URL.EscapedPath()) > maxPathLength || !strings.HasPrefix(r.URL.Path, "/proxyip=") {
		http.Error(w, "invalid websocket path", http.StatusBadRequest)
		return
	}

	proxy, err := parseSocks5(strings.TrimPrefix(r.URL.Path, "/proxyip="))
	if err != nil {
		http.Error(w, "valid socks5 path required", http.StatusBadRequest)
		return
	}

	ws, err := g.upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer ws.Close()
	ws.SetReadLimit(64 * 1024 * 1024)

	early := decodeEarlyData(r.Header.Get("Sec-WebSocket-Protocol"))
	req, err := g.readVlessRequest(ws, early)
	if err != nil || req.command != 1 || !g.authorized(req) {
		_ = ws.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.ClosePolicyViolation, "authentication failed"), time.Now().Add(time.Second))
		return
	}

	remote, err := dialSocks5(r.Context(), proxy, req.host, req.port, req.payload)
	if err != nil {
		_ = ws.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseTryAgainLater, "upstream unavailable"), time.Now().Add(time.Second))
		return
	}
	defer remote.Close()

	done := make(chan struct{})
	go func() {
		defer close(done)
		copyTCPToWebSocket(remote, ws, req.version)
		_ = ws.Close()
	}()

	copyWebSocketToTCP(ws, remote)
	_ = remote.Close()
	<-done
}

func (g *gateway) authorized(req *vlessRequest) bool {
	file, err := os.Open(g.usersFile)
	if err != nil {
		return false
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		parts := strings.SplitN(scanner.Text(), "|", 2)
		candidate := parts[len(parts)-1]
		decoded, err := decodeUUID(strings.TrimSpace(candidate))
		if err == nil && len(req.uuid) == 16 && subtle.ConstantTimeCompare(decoded, req.uuid) == 1 {
			return true
		}
	}
	return false
}

func (g *gateway) readVlessRequest(ws *websocket.Conn, initial []byte) (*vlessRequest, error) {
	buffer := append([]byte(nil), initial...)
	for len(buffer) <= maxInitialLength {
		request, incomplete, err := parseVless(buffer)
		if err != nil {
			return nil, err
		}
		if !incomplete {
			return request, nil
		}
		messageType, data, err := ws.ReadMessage()
		if err != nil {
			return nil, err
		}
		if messageType != websocket.BinaryMessage {
			return nil, errors.New("binary websocket message required")
		}
		buffer = append(buffer, data...)
	}
	return nil, errors.New("initial request too large")
}

func parseVless(data []byte) (*vlessRequest, bool, error) {
	if len(data) < 18 {
		return nil, true, nil
	}
	version := data[0]
	uuid := append([]byte(nil), data[1:17]...)
	optionLength := int(data[17])
	commandIndex := 18 + optionLength
	if len(data) < commandIndex+4 {
		return nil, true, nil
	}
	command := data[commandIndex]
	port := uint16(data[commandIndex+1])<<8 | uint16(data[commandIndex+2])
	addressType := data[commandIndex+3]
	index := commandIndex + 4
	var host string

	switch addressType {
	case 1:
		if len(data) < index+4 {
			return nil, true, nil
		}
		host = net.IP(data[index : index+4]).String()
		index += 4
	case 2:
		if len(data) < index+1 {
			return nil, true, nil
		}
		length := int(data[index])
		index++
		if length == 0 {
			return nil, false, errors.New("invalid destination domain")
		}
		if len(data) < index+length {
			return nil, true, nil
		}
		host = string(data[index : index+length])
		index += length
	case 3:
		if len(data) < index+16 {
			return nil, true, nil
		}
		host = net.IP(data[index : index+16]).String()
		index += 16
	default:
		return nil, false, errors.New("invalid address type")
	}

	if host == "" || port == 0 {
		return nil, false, errors.New("invalid destination")
	}
	return &vlessRequest{
		version: version,
		command: command,
		host:    host,
		port:    port,
		payload: append([]byte(nil), data[index:]...),
		uuid:    uuid,
	}, false, nil
}

func decodeUUID(value string) ([]byte, error) {
	compact := strings.ReplaceAll(strings.ToLower(value), "-", "")
	if len(compact) != 32 {
		return nil, errors.New("invalid uuid")
	}
	decoded, err := hex.DecodeString(compact)
	if err != nil || len(decoded) != 16 {
		return nil, errors.New("invalid uuid")
	}
	return decoded, nil
}

func parseSocks5(value string) (socksConfig, error) {
	if !strings.HasPrefix(strings.ToLower(value), "socks5://") && !strings.HasPrefix(strings.ToLower(value), "socks://") {
		return socksConfig{}, errors.New("socks5 scheme required")
	}

	schemeIndex := strings.Index(value, "://")
	rest := value[schemeIndex+3:]
	auth := ""
	hostPort := rest
	if at := strings.LastIndex(rest, "@"); at >= 0 {
		auth = rest[:at]
		hostPort = rest[at+1:]
	}

	username, password := "", ""
	if auth != "" {
		if colon := strings.Index(auth, ":"); colon >= 0 {
			username, password = auth[:colon], auth[colon+1:]
		} else {
			username = auth
		}
		if decoded, err := url.PathUnescape(username); err == nil {
			username = decoded
		}
		if decoded, err := url.PathUnescape(password); err == nil {
			password = decoded
		}
	}

	host, portText, err := net.SplitHostPort(hostPort)
	if err != nil {
		return socksConfig{}, errors.New("host and port required")
	}
	port64, err := strconv.ParseUint(portText, 10, 16)
	if err != nil || port64 == 0 || host == "" {
		return socksConfig{}, errors.New("invalid socks5 address")
	}
	if len([]byte(username)) > 255 || len([]byte(password)) > 255 {
		return socksConfig{}, errors.New("socks5 credentials too long")
	}
	return socksConfig{host: host, port: uint16(port64), username: username, password: password}, nil
}

func dialSocks5(ctx context.Context, proxy socksConfig, targetHost string, targetPort uint16, initial []byte) (net.Conn, error) {
	dialCtx, cancel := context.WithTimeout(ctx, connectTimeout)
	defer cancel()

	proxyIP, err := resolveProxyIP(dialCtx, proxy.host)
	if err != nil {
		return nil, err
	}
	dialer := net.Dialer{Timeout: connectTimeout, KeepAlive: 30 * time.Second}
	conn, err := dialer.DialContext(dialCtx, "tcp", net.JoinHostPort(proxyIP.String(), strconv.Itoa(int(proxy.port))))
	if err != nil {
		return nil, err
	}
	success := false
	defer func() {
		if !success {
			_ = conn.Close()
		}
	}()

	_ = conn.SetDeadline(time.Now().Add(handshakeTimeout))
	reader := bufio.NewReaderSize(conn, 512)
	needsAuth := proxy.username != "" || proxy.password != ""
	if needsAuth {
		_, err = conn.Write([]byte{5, 2, 0, 2})
	} else {
		_, err = conn.Write([]byte{5, 1, 0})
	}
	if err != nil {
		return nil, err
	}

	method := make([]byte, 2)
	if _, err = io.ReadFull(reader, method); err != nil || method[0] != 5 || method[1] == 255 {
		return nil, errors.New("socks5 method rejected")
	}
	if method[1] == 2 {
		user := []byte(proxy.username)
		pass := []byte(proxy.password)
		auth := make([]byte, 0, 3+len(user)+len(pass))
		auth = append(auth, 1, byte(len(user)))
		auth = append(auth, user...)
		auth = append(auth, byte(len(pass)))
		auth = append(auth, pass...)
		if _, err = conn.Write(auth); err != nil {
			return nil, err
		}
		reply := make([]byte, 2)
		if _, err = io.ReadFull(reader, reply); err != nil || reply[1] != 0 {
			return nil, errors.New("socks5 authentication failed")
		}
	} else if method[1] != 0 {
		return nil, errors.New("unsupported socks5 authentication")
	}

	request, err := buildSocksRequest(targetHost, targetPort)
	if err != nil {
		return nil, err
	}
	if _, err = conn.Write(request); err != nil {
		return nil, err
	}
	replyHead := make([]byte, 4)
	if _, err = io.ReadFull(reader, replyHead); err != nil || replyHead[0] != 5 || replyHead[1] != 0 {
		return nil, errors.New("socks5 destination refused")
	}
	switch replyHead[3] {
	case 1:
		_, err = io.CopyN(io.Discard, reader, 6)
	case 4:
		_, err = io.CopyN(io.Discard, reader, 18)
	case 3:
		length, readErr := reader.ReadByte()
		if readErr != nil {
			return nil, readErr
		}
		_, err = io.CopyN(io.Discard, reader, int64(length)+2)
	default:
		return nil, errors.New("invalid socks5 reply")
	}
	if err != nil {
		return nil, err
	}
	if len(initial) > 0 {
		if _, err = conn.Write(initial); err != nil {
			return nil, err
		}
	}
	_ = conn.SetDeadline(time.Time{})
	success = true
	return conn, nil
}

var resolveProxyIP = resolvePublicIP

func resolvePublicIP(ctx context.Context, host string) (net.IP, error) {
	if literal := net.ParseIP(host); literal != nil {
		if isPublicIP(literal) {
			return literal, nil
		}
		return nil, errors.New("private socks5 endpoint blocked")
	}
	addresses, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, err
	}
	for _, address := range addresses {
		if isPublicIP(address.IP) {
			return address.IP, nil
		}
	}
	return nil, errors.New("socks5 endpoint has no public address")
}

func isPublicIP(ip net.IP) bool {
	return ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback() && !ip.IsLinkLocalUnicast() && !ip.IsLinkLocalMulticast() && !ip.IsUnspecified()
}

func buildSocksRequest(host string, port uint16) ([]byte, error) {
	request := []byte{5, 1, 0}
	if ip := net.ParseIP(host); ip != nil {
		if ipv4 := ip.To4(); ipv4 != nil {
			request = append(request, 1)
			request = append(request, ipv4...)
		} else {
			request = append(request, 4)
			request = append(request, ip.To16()...)
		}
	} else {
		domain := []byte(host)
		if len(domain) == 0 || len(domain) > 255 {
			return nil, errors.New("invalid destination domain")
		}
		request = append(request, 3, byte(len(domain)))
		request = append(request, domain...)
	}
	request = append(request, byte(port>>8), byte(port))
	return request, nil
}

func copyWebSocketToTCP(ws *websocket.Conn, conn net.Conn) {
	buffer := make([]byte, copyBufferSize)
	for {
		messageType, reader, err := ws.NextReader()
		if err != nil {
			return
		}
		if messageType != websocket.BinaryMessage {
			continue
		}
		if _, err = io.CopyBuffer(conn, reader, buffer); err != nil {
			return
		}
	}
}

func copyTCPToWebSocket(conn net.Conn, ws *websocket.Conn, version byte) {
	buffer := make([]byte, copyBufferSize)
	first := true
	for {
		n, err := conn.Read(buffer)
		if n > 0 {
			writer, writeErr := ws.NextWriter(websocket.BinaryMessage)
			if writeErr != nil {
				return
			}
			if first {
				first = false
				_, writeErr = writer.Write([]byte{version, 0})
			}
			if writeErr == nil {
				_, writeErr = writer.Write(buffer[:n])
			}
			closeErr := writer.Close()
			if writeErr != nil || closeErr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

func decodeEarlyData(header string) []byte {
	if header == "" {
		return nil
	}
	value := strings.TrimSpace(strings.Split(header, ",")[0])
	for _, encoding := range []*base64.Encoding{base64.RawURLEncoding, base64.URLEncoding, base64.RawStdEncoding, base64.StdEncoding} {
		if decoded, err := encoding.DecodeString(value); err == nil {
			return decoded
		}
	}
	return nil
}
