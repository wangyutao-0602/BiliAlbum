#!/usr/bin/env python3
"""Bounded, anonymous metadata diagnostics. Never downloads media or reads cookies."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bilialbum.api import BiliAPI
from bilialbum.downloader import assert_identity
from bilialbum.extraction import RequestTrace, SilentLogger, extract_single, metadata_options
from bilialbum.models import BiliAlbumError, Episode, NetworkFailure
from bilialbum.network import exception_details, proxy_summary, safe_url, validate_proxies
from bilialbum.urltools import parse_url, _SafeRedirect

LIMIT = 8_000_000


class RedirectRecorder(_SafeRedirect):
    def __init__(self):
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append({'status': code, 'url': safe_url(newurl)})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_http(url: str, params: dict, kind: str, *, direct=False) -> dict:
    row = {'target': safe_url(url), 'method': 'GET', 'status': None, 'exception': None,
           'final_url': safe_url(url), 'redirected': False, 'redirects': [],
           'valid_data': False, 'failure_stage': None,
           'network_mode': 'explicit_direct' if direct else 'system_or_environment'}
    redirects = RedirectRecorder()
    headers = {'User-Agent': BiliAPI.USER_AGENT, 'Referer': 'https://www.bilibili.com/',
               'Accept': 'application/json, text/plain, */*' if kind == 'api'
                         else 'text/html,application/xhtml+xml'}
    try:
        if not direct:
            validate_proxies()
        handlers = [redirects, urllib.request.ProxyHandler({})] if direct else [redirects]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(urllib.request.Request(url, headers=headers), timeout=12) as response:
            row.update(status=response.status, final_url=safe_url(response.geturl()))
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('size limit')
        if kind == 'api':
            payload = json.loads(raw)
            row['business_code'] = payload.get('code') if isinstance(payload, dict) else None
            data = BiliAPI.parse_api_payload(payload)
        else:
            data = BiliAPI.parse_public_page(raw, params)
        if not data.get('bvid'):
            raise BiliAlbumError('missing bvid')
        for key in ('bvid', 'aid'):
            if key in params and str(data.get(key)) != str(params[key]):
                raise BiliAlbumError('identity mismatch')
        row['valid_data'] = True
    except Exception as exc:
        status, category = exception_details(exc)
        if isinstance(exc, NetworkFailure):
            category = exc.category
        row.update(exception=type(exc).__name__, category=category,
                   failure_stage=('HTTP 请求' if status or row['status'] is None else '响应元数据校验'))
        if isinstance(exc, NetworkFailure) and category == 'invalid_proxy':
            row['failure_stage'] = exc.stage
        if status:
            row.update(status=status, final_url=safe_url(getattr(exc, 'url', url)))
        # No exception messages, headers, cookies, HTML or response bodies are emitted.
    row.update(redirected=bool(redirects.hops), redirects=redirects.hops)
    return row


def environment_report(*, direct=False) -> dict:
    env = {name: proxy_summary({'no' if name.lower() == 'no_proxy' else 'proxy': os.environ[name]})
           for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                        'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') if name in os.environ}
    # Snapshot before yt-dlp applies its HTTP-to-HTTPS compatibility fallback.
    proxies = dict(urllib.request.getproxies())
    report = {'python': platform.python_version(), 'executable': sys.executable,
              'platform': platform.platform(), 'openssl': ssl.OPENSSL_VERSION,
              'proxy_environment': env, 'urllib_proxies': proxy_summary(proxies),
              'urllib_effective_proxies': proxy_summary({} if direct else proxies),
              'requested_network_mode': 'explicit_direct' if direct else 'system_or_environment',
              'urllib_proxy_source': 'environment' if urllib.request.getproxies_environment()
                                     else 'system_or_direct',
              'proxy_validation': 'ok', 'dependencies': {},
              'notes': ['浏览器的 PAC、扩展、VPN/TUN 分流及实际出口需在浏览器和网络管理工具中核实；代理表相同不证明出口相同。',
                        'urllib 不会像 yt-dlp 一样将 ALL_PROXY 用于所有协议；yt-dlp 还会在只有 HTTP 代理时补充 HTTPS。']}
    try:
        validate_proxies(proxies)
    except NetworkFailure:
        report['proxy_validation'] = 'invalid_proxy'
    for name in ('yt-dlp', 'requests', 'urllib3', 'certifi', 'curl-cffi', 'brotli', 'websockets', 'pytest'):
        try:
            report['dependencies'][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            report['dependencies'][name] = None
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils.networking import select_proxy
        with YoutubeDL({**metadata_options(direct=direct), 'logger': SilentLogger()}) as ydl:
            report['yt_dlp_proxies'] = proxy_summary(ydl.proxies)
            report['yt_dlp_same_proxy_table'] = proxies == ydl.proxies
            report['yt_dlp_request_handlers'] = list(ydl._request_director.handlers)
            report['effective_https_routes'] = {}
            for host in ('api.bilibili.com', 'www.bilibili.com', 'b23.tv'):
                python_proxy = None if direct or urllib.request.proxy_bypass(host) else proxies.get('https')
                ydl_proxy = select_proxy('https://' + host + '/', ydl.proxies)
                if ydl_proxy == '__noproxy__':
                    ydl_proxy = None
                report['effective_https_routes'][host] = {
                    'urllib': proxy_summary({'https': python_proxy}) if python_proxy else {'direct': True},
                    'yt_dlp': proxy_summary({'https': ydl_proxy}) if ydl_proxy else {'direct': True},
                    'same_selected_proxy': python_proxy == ydl_proxy}
    except Exception as exc:
        report['yt_dlp_environment_error'] = type(exc).__name__
    if sys.platform == 'darwin':
        try:
            proc = subprocess.run(['scutil', '--proxy'], capture_output=True, text=True, timeout=5)
            # Only report switches, ports and whether proxy hosts are loopback. No PAC URLs/credentials.
            switches = dict(re.findall(r'^\s*(\w+(?:Enable|Port))\s*:\s*(\d+)\s*$', proc.stdout, re.M))
            hosts = re.findall(r'^\s*(HTTPProxy|HTTPSProxy|SOCKSProxy)\s*:\s*(\S+)\s*$', proc.stdout, re.M)
            report['macos_system_proxy'] = {'switches': switches,
                'hosts': {k: {'loopback': v in ('127.0.0.1', 'localhost', '::1'), 'host': '<redacted>'} for k, v in hosts},
                'readable': bool(switches or hosts)}
        except (OSError, subprocess.TimeoutExpired) as exc:
            report['macos_system_proxy'] = {'exception': type(exc).__name__}
    for command in ('ffmpeg', 'ffprobe'):
        path = shutil.which(command)
        if path:
            try:
                proc = subprocess.run([path, '-version'], capture_output=True, text=True, timeout=5)
                report[command] = proc.stdout.splitlines()[0]
            except (OSError, subprocess.TimeoutExpired):
                report[command] = 'unavailable'
        else:
            report[command] = 'not installed'
    return report


def check_yt_dlp(url: str, *, direct=False) -> dict:
    trace = RequestTrace(limit=10)
    row = {'target': safe_url(url), 'method': 'extract_info(download=False)',
           'status': None, 'exception': None, 'final_url': safe_url(url), 'redirected': None,
           'valid_data': False, 'failure_stage': None, 'requests': trace.records,
           'network_mode': 'explicit_direct' if direct else 'system_or_environment'}
    validating = False
    try:
        from yt_dlp import YoutubeDL
        info = extract_single(YoutubeDL, url, trace=trace, direct=direct)
        validating = True
        if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video'):
            raise BiliAlbumError('not a single video')
        target = parse_url(url)
        # av links resolve to BV in the official extractor; only accept a BV identity.
        if target.video_id.startswith('BV'):
            assert_identity(info, Episode(1, '', target.video_id, target.page))
        else:
            match = re.fullmatch(r'(BV[A-Za-z0-9]{10})(?:_p\d+)?', str(info.get('id', '')))
            if not match:
                raise BiliAlbumError('invalid BV identity')
            assert_identity(info, Episode(1, '', match[1], target.page))
        if not info.get('title') or not info.get('formats'):
            raise BiliAlbumError('no playable formats')
        row.update(valid_data=True, id=info['id'])
    except Exception as exc:
        failure = (NetworkFailure('yt-dlp 元数据校验', 'invalid_metadata', '元数据校验失败。', retryable=False)
                   if validating else exc if isinstance(exc, NetworkFailure) else trace.failure(exc))
        row.update(exception=type(exc).__name__, status=failure.status,
                   category=failure.category, failure_stage=failure.stage)
    if trace.records:
        last = trace.records[-1]
        row.update(final_url=last['final_url'],
                   redirected=any(r['redirected'] is True for r in trace.records))
        if row['status'] is None:
            row['status'] = last['status']
    return row


def run_yt_dlp(url: str, *, direct=False) -> dict:
    # Subprocess bounds total extraction time; the Python API ignores CLI config files.
    try:
        command = [sys.executable, str(Path(__file__).resolve()), url, '--_yt-worker']
        if direct:
            command.append('--direct')
        proc = subprocess.run(command,
                              capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        category = 'worker_error'
    except subprocess.TimeoutExpired:
        category = 'timeout'
    except (OSError, ValueError):
        category = 'worker_error'
    return {'target': safe_url(url), 'method': 'extract_info(download=False)', 'status': None,
            'exception': category, 'final_url': None, 'redirected': None, 'valid_data': False,
            'failure_stage': 'yt-dlp 子进程', 'requests': []}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='BiliAlbum 少量只读网络诊断（不下载，不读 Cookie）')
    parser.add_argument('url', nargs='?', help='具体 BV/av 视频链接')
    parser.add_argument('--yt-dlp', action='store_true', help='独立检查 yt-dlp；总时限 60 秒，最多 10 次请求，无重试')
    parser.add_argument('--settings-only', action='store_true', help='只读本机设置，不访问网站')
    parser.add_argument('--direct', action='store_true', help='所有检查仅在本进程禁用显式代理，不更改系统或 VPN')
    parser.add_argument('--compare-direct', action='store_true',
                        help='额外一次 API 直连对照，仅对本次请求禁用显式代理；不更改系统/VPN，不自动用于解析或下载')
    parser.add_argument('--output', type=Path, help='保存脱敏 JSON；标准输出始终也是 JSON')
    parser.add_argument('--_yt-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.direct and args.compare_direct:
        parser.error('--direct 与 --compare-direct 请分开使用，避免重复直连请求。')
    try:
        target = parse_url(args.url or '') if not args.settings_only else None
        if target and not target.video_id:
            raise BiliAlbumError('请输入具体 BV/av 视频链接。')
    except (BiliAlbumError, ValueError) as exc:
        parser.error(str(exc))
    if args._yt_worker:
        print(json.dumps(check_yt_dlp(target.url, direct=args.direct), ensure_ascii=False))
        return 0
    report = {'time_utc': datetime.now(timezone.utc).isoformat(), 'environment': environment_report(direct=args.direct),
              'policy': '匿名元数据；API/网页各一次；yt-dlp 可选且有请求/时间上限；不自动切换代理或直连',
              'checks': {}}
    if target:
        page_url = f'https://www.bilibili.com/video/{target.video_id}/'
        params = {'bvid': target.video_id} if target.video_id.startswith('BV') else {'aid': target.video_id[2:]}
        report['checks']['api'] = check_http(BiliAPI.BASE + '/x/web-interface/view?' + urllib.parse.urlencode(params), params, 'api', direct=args.direct)
        report['checks']['webpage'] = check_http(page_url, params, 'webpage', direct=args.direct)
        if args.compare_direct:
            report['checks']['api_direct_comparison'] = check_http(
                BiliAPI.BASE + '/x/web-interface/view?' + urllib.parse.urlencode(params), params, 'api', direct=True)
        if args.yt_dlp:
            report['checks']['yt_dlp'] = run_yt_dlp(page_url + '?p=' + str(target.page), direct=args.direct)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    healthy = report['environment'].get('proxy_validation') != 'invalid_proxy' or args.direct
    return 0 if healthy and all(r['valid_data'] for r in report['checks'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
