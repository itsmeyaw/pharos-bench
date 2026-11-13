#!/usr/bin/env python3
"""Synthetic Kubernetes API for the `kubernetes` plugin of both DNS servers: --services Services and EndpointSlices
in namespace `bench`, the same objects as pharos-kubernetes examples/memory.rs but with a configurable count.
Lists and initial watch events are streamed (chunked) so large clusters cost no RAM here. Watches stay open and
never emit further events."""
import argparse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

KINDS = {
    "/api/v1/services": ("v1", "Service"),
    "/apis/discovery.k8s.io/v1/endpointslices": ("discovery.k8s.io/v1", "EndpointSlice"),
    "/api/v1/namespaces": ("v1", "Namespace"),
}
VERBS = '"verbs":["get","list","watch"]'
STATIC = {
    "/version": '{"major":"1","minor":"37","gitVersion":"v1.37.0","gitCommit":"synthetic","gitTreeState":"clean","buildDate":"2026-01-01T00:00:00Z","goVersion":"go1","compiler":"gc","platform":"linux/amd64"}',
    "/api": '{"kind":"APIVersions","apiVersion":"v1","versions":["v1"]}',
    "/apis": '{"kind":"APIGroupList","apiVersion":"v1","groups":[{"name":"discovery.k8s.io","versions":[{"groupVersion":"discovery.k8s.io/v1","version":"v1"}],"preferredVersion":{"groupVersion":"discovery.k8s.io/v1","version":"v1"}}]}',
    "/api/v1": '{"kind":"APIResourceList","apiVersion":"v1","groupVersion":"v1","resources":[{"name":"services","namespaced":true,"kind":"Service",' + VERBS + '},{"name":"namespaces","namespaced":false,"kind":"Namespace",' + VERBS + "}]}",
    "/apis/discovery.k8s.io": '{"kind":"APIGroup","apiVersion":"v1","name":"discovery.k8s.io","versions":[{"groupVersion":"discovery.k8s.io/v1","version":"v1"}],"preferredVersion":{"groupVersion":"discovery.k8s.io/v1","version":"v1"}}',
    "/apis/discovery.k8s.io/v1": '{"kind":"APIResourceList","apiVersion":"v1","groupVersion":"discovery.k8s.io/v1","resources":[{"name":"endpointslices","namespaced":true,"kind":"EndpointSlice",' + VERBS + "}]}",
    "/healthz": "ok",
    "/readyz": "ok",
}
NOT_FOUND = '{"apiVersion":"v1","kind":"Status","status":"Failure","reason":"NotFound","code":404}'
BATCH = 1000


def obj(kind, i):
    if kind == "Service":
        ip = f"10.{i // 65536}.{i // 256 % 256}.{i % 256}"
        return ('{"apiVersion":"v1","kind":"Service","metadata":{"name":"svc%d","namespace":"bench","resourceVersion":"1"},'
                '"spec":{"type":"ClusterIP","clusterIP":"%s","clusterIPs":["%s"],"ports":[{"port":80,"protocol":"TCP"}]}}' % (i, ip, ip))
    if kind == "EndpointSlice":
        addrs = ",".join('{"addresses":["192.%d.%d.%d"]}' % (a // 65536, a // 256 % 256, a % 256) for a in range(i * 3, i * 3 + 3))
        return ('{"apiVersion":"discovery.k8s.io/v1","kind":"EndpointSlice","metadata":{"name":"slice%d","namespace":"bench",'
                '"resourceVersion":"1","labels":{"kubernetes.io/service-name":"svc%d"}},"addressType":"IPv4","endpoints":[%s]}' % (i, i, addrs))
    return '{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"bench","resourceVersion":"1"}}'


def batches(path, services, watch):
    """Body of a list (watch=False) or of a sendInitialEvents watch, as an iterator of strings."""
    api, kind = KINDS[path]
    count = 1 if kind == "Namespace" else services
    if not watch:
        yield '{"apiVersion":"%s","kind":"%sList","metadata":{"resourceVersion":"1"},"items":[' % (api, kind)
    for start in range(0, count, BATCH):
        objs = [obj(kind, i) for i in range(start, min(start + BATCH, count))]
        if watch:
            yield "".join('{"type":"ADDED","object":' + o + "}\n" for o in objs)
        else:
            yield ("," if start else "") + ",".join(objs)
    if watch:
        yield ('{"type":"BOOKMARK","object":{"apiVersion":"%s","kind":"%s","metadata":{"resourceVersion":"1",'
               '"annotations":{"k8s.io/initial-events-end":"true"}}}}\n' % (api, kind))
    else:
        yield "]}"


def handler(services):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, code, body):
            data = body.encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            if u.path not in KINDS:
                return self.reply(200, STATIC[u.path]) if u.path in STATIC else self.reply(404, NOT_FOUND)
            watch = q.get("watch") == ["true"]
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            try:
                if not watch or q.get("sendInitialEvents") == ["true"]:
                    for part in batches(u.path, services, watch):
                        data = part.encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                if not watch:
                    self.wfile.write(b"0\r\n\r\n")
                    return
                while True:
                    time.sleep(60)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=6443)
    ap.add_argument("--services", type=int, default=10000)
    a = ap.parse_args()
    ThreadingHTTPServer(("0.0.0.0", a.port), handler(a.services)).serve_forever()
