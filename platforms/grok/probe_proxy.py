"""探测 45.192.243.14:8888 代理协议（SOCKS5 握手 / HTTP CONNECT / HTTP GET）"""
import socket


def probe(payload, timeout=8):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("45.192.243.14", 8888))
        s.sendall(payload)
        return s.recv(120)
    except Exception as e:
        return "ERR:" + str(e)[:60]
    finally:
        s.close()


print("SOCKS5握手:", probe(b"\x05\x01\x00"))
print("HTTP CONNECT:", probe(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"))
print("HTTP GET:", probe(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"))
