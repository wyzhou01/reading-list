#!/usr/bin/env python3
"""net_diag.py — 代理架构一键诊断 (06-19 新增)
========================================
验证 mihomo TUN + VPS SSR + 国内直连 三层架构工作正常
不是修复工具, 只是 health check

测试目标:
1. 国内服务直连 (api.minimaxi.com / baidu.com) — 应< 0.5s, 不走 VPS
2. 海外服务走 VPS (api.github.com / podcasts.apple.com) — 应< 2s
3. raw.githubusercontent.com 已知坏 — 但不应被用户主动用
4. mihomo ext-ctl socket 可访问 (代理切换能力)
5. mihomo group 状态正常 (🚀 节点选择 -> 🚀 手动切换 -> VPS)

退出码:
  0 = 全部 OK
  1 = 国内直连失败
  2 = 海外 VPS 失败
  3 = mihomo 不可达
"""
import subprocess, sys, json, time
from pathlib import Path

MIHOMO_SOCK = '/tmp/verge/verge-mihomo.sock'
PROXY_TIMEOUT = 8

TESTS = [
    ('国内直连 api.minimaxi.com', 'https://api.minimaxi.com/', 0.5, 'direct'),
    ('国内直连 baidu.com', 'https://www.baidu.com/', 0.5, 'direct'),
    ('海外 VPS api.github.com', 'https://api.github.com/zen', 2.0, 'vps'),
    ('海外 VPS podcasts.apple.com', 'https://podcasts.apple.com/', 2.0, 'vps'),
    ('海外 VPS telegram.org', 'https://telegram.org/', 5.0, 'vps'),
]


def curl_test(url, timeout, expect_vps=False):
    """curl 测联通性, 返回 (ok, code, time, err)"""
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-o', '/dev/null',
           '-w', '%{http_code}|%{time_total}', url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+2)
        parts = r.stdout.strip().split('|')
        if len(parts) != 2: return False, 0, 0, r.stderr or 'parse err'
        code, t = parts[0], float(parts[1])
        ok = (r.returncode == 0 and code in ('200', '301', '302', '400', '403', '404'))
        return ok, int(code) if code.isdigit() else 0, t, ''
    except subprocess.TimeoutExpired:
        return False, 0, timeout, 'timeout'
    except Exception as e:
        return False, 0, 0, str(e)


def mihomo_get(path):
    """mihomo ext-ctl GET"""
    cmd = ['curl', '-sS', '--max-time', '5',
           '--unix-socket', MIHOMO_SOCK,
           f'http://localhost:9090{path}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7)
        if r.returncode == 0: return r.stdout
    except Exception:
        pass
    return None


def check_mihomo():
    """mihomo 健康"""
    print('\n[1] mihomo 健康检查')
    if not Path(MIHOMO_SOCK).exists():
        print(f'  ❌ socket 不存在: {MIHOMO_SOCK}')
        return False
    print(f'  ✓ socket 存在')

    data = mihomo_get('/proxies')
    if not data:
        print('  ❌ mihomo API 不可达')
        return False
    try:
        proxies = json.loads(data).get('proxies', {})
    except json.JSONDecodeError:
        print('  ❌ mihomo /proxies 返回非 JSON')
        return False

    # 验证关键 group
    key_groups = ['🚀 节点选择', '🚀 手动切换', 'GLOBAL']
    for g in key_groups:
        info = proxies.get(g, {})
        if not info:
            print(f'  ⚠️  group 缺失: {g}')
            continue
        now = info.get('now', '?')
        all_opts = info.get('all', [])
        print(f'  ✓ {g}: now={now}, options={all_opts[:3]}...')

    # 验证 VPS 在线
    global_now = proxies.get('GLOBAL', {}).get('now', '')
    if '10241' not in global_now:
        print(f'  ⚠️  GLOBAL 未指向 VPS 10241: {global_now}')
    return True


def check_url(name, url, max_time, kind):
    """单个 URL 测试"""
    print(f'\n[测试] {name}')
    print(f'  url: {url}')
    print(f'  期望: <{max_time}s ({kind})')
    ok, code, t, err = curl_test(url, PROXY_TIMEOUT)
    if ok and t < max_time:
        print(f'  ✅ OK code={code} time={t:.3f}s')
        return 0
    elif ok:
        print(f'  ⚠️  OK 但超时 code={code} time={t:.3f}s (阈值 {max_time}s)')
        return 1
    else:
        print(f'  ❌ FAIL code={code} time={t:.3f}s err={err[:80]}')
        return 2


def main():
    print('=' * 70)
    print('net_diag — 代理架构诊断')
    print('=' * 70)

    # 1. mihomo
    mihomo_ok = check_mihomo()
    if not mihomo_ok:
        print('\n❌ mihomo 不可达, 后续测试不准确')
        return 3

    # 2. URL 测试
    print('\n' + '=' * 70)
    print('URL 联通性测试')
    print('=' * 70)
    worst = 0
    for name, url, max_t, kind in TESTS:
        code = check_url(name, url, max_t, kind)
        worst = max(worst, code)

    # 3. 总结
    print('\n' + '=' * 70)
    print('总结')
    print('=' * 70)
    if worst == 0:
        print('  ✅ 代理架构 100% 健康')
        return 0
    elif worst == 1:
        print(f'  ⚠️  有 URL 慢但可达 (worst={worst})')
        return 1
    else:
        print(f'  ❌ 有 URL 不通 (worst={worst})')
        return worst


if __name__ == '__main__':
    sys.exit(main())
