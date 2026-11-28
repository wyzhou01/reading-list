#!/usr/bin/env python3
"""
generate.py — M3 生成"本书结构 + 分集" + "第 X 集内容"
====================================================
两阶段:

阶段 1 (只跑一次, 第一次 cron 触发时跑):
  - 输入: 整本书 + 书名
  - 输出: book_structure.json (含分集方案, 建议 N 集)

阶段 2 (每个集跑一次, 后续 cron 触发跑):
  - 输出: episode_N.json + episode_N.txt (本集内容)

用法:
  # 阶段 1 (第一次跑新书)
  python3 generate.py --phase 1 <extracted_txt>

  # 阶段 2 (跑第 X 集)
  python3 generate.py --phase 2 <extracted_txt> --episode 1

TTS 朗读硬约束（写进 prompt）:
  - 数字 → 汉字
  - 年份 → 拆位
  - 英文 → 拆字母/拼读
  - 避免长句
  - 标点控节奏

# 2026-06-08 21:51: 重构 (从损坏的备份中提取, 用 .format() 替换 f-string)
# - 4 个 f-string triple-quote 块改为 triple-quote + .format()  (line 175, head_prompt, tail_prompt)
# - 单层 f-string ({episode_no} etc) 转义为 {{}}
# - extract_json 加 JSON 截断修复
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib import request, error


REPO_ROOT = Path(__file__).resolve().parent.parent
load_env = None  # 2026-06-08: 兼容旧代码, 但实际不用

# 进程级多厂商熔断状态表: {provider_name: cooldown_until_timestamp}
_CIRCUIT_BREAKER = {}
ALERT_402_FILE = REPO_ROOT / "state" / "minimax_402_alert.json"

def trigger_402_watchdog(provider: str, reason: str):
    """记录 402 计费熔断状态至文件，供 daily_pick 等调度器联动控速与告警"""
    try:
        ALERT_402_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "provider": provider,
            "reason": reason,
            "status": "triggered",
            "timestamp": time.time(),
            "time_str": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S BJT"),
            "cooldown_until": time.time() + 6 * 3600,
        }
        ALERT_402_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"🔒 402 看门狗警报已持久化写入: {ALERT_402_FILE}", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ 记录 402 看门狗状态失败: {e}", file=sys.stderr)

import html
import subprocess
import time
from datetime import datetime, timezone, timedelta

# 当前处理书籍名称 (用于告警上下文追溯)
_CURRENT_BOOK_TITLE = ""
# 近期已发告警缓存，防止重试或级联报错导致短时间内重复刷屏
_RECENT_ALERTS = {}

def set_current_book_title(title: str):
    """设置当前正在处理的书名"""
    global _CURRENT_BOOK_TITLE
    if title:
        _CURRENT_BOOK_TITLE = title

def _resolve_book_title(prompt_or_messages=None) -> str:
    """提取或推断当前书名，优先全局设定，其次正则解析 prompt 内容"""
    global _CURRENT_BOOK_TITLE
    if _CURRENT_BOOK_TITLE:
        return _CURRENT_BOOK_TITLE
    
    text = ""
    if isinstance(prompt_or_messages, str):
        text = prompt_or_messages
    elif isinstance(prompt_or_messages, list) and prompt_or_messages:
        text = str(prompt_or_messages[-1].get("content", ""))
    
    if text:
        m = re.search(r"(?:书名[：:]\s*|为《|写《)([^》\n\r]+)", text)
        if m:
            extracted = m.group(1).strip()
            if extracted:
                return extracted
    return "未知书籍"

def send_tg_alert(message: str, chat_id: str = "299246410") -> bool:
    """复用 reading-list TG 通道发送告警 (通过 curl 调用 Telegram Bot API)"""
    _load_openclaw_env()
    bot_token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        env_file = Path.home() / ".openclaw" / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("TG_BOT_TOKEN=") or line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not bot_token:
        print("  ⚠️  TG_BOT_TOKEN / TELEGRAM_BOT_TOKEN 未配置，跳过 TG 发送", file=sys.stderr)
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = subprocess.run(
            [
                "curl", "-sS", "--max-time", "20", "-X", "POST",
                "-H", "Content-Type: application/json",
                "--data-binary", json.dumps(body),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if r.returncode != 0:
            print(f"  ⚠️  TG 告警发送失败 (rc={r.returncode}): {r.stderr[:200]}", file=sys.stderr)
            return False
        elif '"ok":true' not in r.stdout:
            print(f"  ⚠️  TG 告警接口返回非 ok: {r.stdout[:200]}", file=sys.stderr)
            return False
        else:
            print(f"  ✓ TG 告警已送达 {chat_id}")
            return True
    except Exception as e:
        print(f"  ⚠️  TG 发送异常: {e}", file=sys.stderr)
        return False

def send_generation_alert(book_title: str = "", reason: str = "", is_test: bool = False) -> bool:
    """上游生成失败 TG 告警
    内容包含: 书名、失败原因、时间
    """
    if not book_title:
        book_title = _resolve_book_title()
        
    now_ts = time.time()
    dedup_key = f"{book_title}:{reason[:60]}"
    if not is_test and dedup_key in _RECENT_ALERTS and (now_ts - _RECENT_ALERTS[dedup_key] < 120):
        return False
    _RECENT_ALERTS[dedup_key] = now_ts

    BJT = timezone(timedelta(hours=8))
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S BJT")
    
    prefix = "🧪 <b>【测试告警·请忽略】</b>" if is_test else "🚨 <b>【好好读书·上游生成失败告警】</b>"
    msg = (
        f"{prefix}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📖 <b>书名</b>: {html.escape(str(book_title))}\n"
        f"⚠️ <b>失败原因</b>: {html.escape(str(reason))}\n"
        f"🕒 <b>时间</b>: {now_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"请及时排查上游生成服务状态与 API 额度。"
    )
    return send_tg_alert(msg)

def _load_openclaw_env():
    """安全预载 ~/.openclaw/.env 环境变量，获取多厂商备用 API 密钥"""
    env_file = Path.home() / ".openclaw" / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

def seconds_until_minimax_reset() -> int:
    """计算距离下一个 MiniMax 5小时重置窗口 (00:00, 05:00, 10:00, 15:00, 20:00 BJT) 的等待秒数"""
    from datetime import datetime, timezone, timedelta
    BJT = timezone(timedelta(hours=8))
    now = datetime.now(BJT)
    reset_hours = [0, 5, 10, 15, 20, 24]
    for h in reset_hours:
        if h > now.hour or (h == now.hour and now.minute == 0 and now.second == 0):
            target = now.replace(hour=h % 24, minute=0, second=0, microsecond=0)
            if h == 24:
                target += timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            return max(int(wait_sec) + 15, 60)
    return 1800


def call_llm_with_failover(prompt_or_messages, max_tokens: int = 4096, temperature: float = 0.5, timeout: int = 90) -> str:
    """多厂商高可用容灾网关 (DeepSeek-V3 -> DashScope / Qwen -> Claude 3.5 Haiku)
    带进程级熔断保护：一旦某厂商返回 402 (余额耗尽) 或 billing 配额触顶，立即熔断 6 小时并自动降级到下一厂商。
    """
    import time
    _load_openclaw_env()
    now_ts = time.time()
    errors = []

    if isinstance(prompt_or_messages, str):
        messages = [{"role": "user", "content": prompt_or_messages}]
    else:
        messages = prompt_or_messages

    providers = [
        {
            "name": "DeepSeek-V3",
            "url": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-chat",
            "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        },
        {
            "name": "DashScope-Qwen",
            "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "qwen-plus",
            "api_key": os.environ.get("DASHSCOPE_API_KEY", ""),
        },
        {
            "name": "Claude-3.5-Haiku",
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "model": "anthropic/claude-3.5-haiku",
            "api_key": os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""),
        },
    ]

    for prov in providers:
        pname = prov["name"]
        key = prov["api_key"]
        if not key:
            continue  # 未配置 Key 的厂商跳过

        if _CIRCUIT_BREAKER.get(pname, 0) > now_ts:
            remain = int(_CIRCUIT_BREAKER[pname] - now_ts)
            print(f"⏳ 厂商 {pname} 处于 402 计费熔断保护期 (剩余 {remain}s)，跳过", file=sys.stderr)
            continue

        print(f"🔄 [多厂商容灾网关] 路由切流至备用厂商: {pname} ({prov['model']})...", file=sys.stderr)
        # DeepSeek 与 Claude 3.5 Haiku 等标准接口单次生成最大限制为 8192 tokens，防止 16000 触发 400 Bad Request
        effective_max_tokens = min(max_tokens, 8192)
        for attempt in range(2):
            try:
                body = {
                    "model": prov["model"],
                    "messages": messages,
                    "max_tokens": effective_max_tokens,
                    "temperature": temperature,
                }
                req = request.Request(
                    prov["url"],
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with request.urlopen(req, timeout=timeout) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    content = resp_data["choices"][0]["message"]["content"]
                    print(f"✅ [多厂商容灾网关] {pname} 成功返回响应！", file=sys.stderr)
                    return content
            except error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    err_text = str(e.reason)

                # HTTP 402 或账单配额耗尽：立即熔断该厂商 6 小时 (兼容中英文账单/额度错误关键词)
                is_billing_err = (
                    e.code == 402 or
                    any(k in err_text.lower() for k in ("billing", "insufficient", "quota", "balance", "exhausted", "overdue", "unpaid")) or
                    any(k in err_text for k in ("余额", "欠费", "额度不足", "充值"))
                )
                if is_billing_err:
                    _CIRCUIT_BREAKER[pname] = time.time() + 6 * 3600
                    msg = f"{pname}: HTTP {e.code} Billing/Quota Exhausted (熔断 6 小时)"
                    print(f"🚨 {msg}", file=sys.stderr)
                    errors.append(msg)
                    send_generation_alert(_resolve_book_title(messages), f"备用厂商 {pname} 触发 402 计费熔断: {msg}")
                    break

                # HTTP 429 或 5xx：指数退避重试 1 次后切流
                if e.code in (429, 500, 502, 503, 504) and attempt < 1:
                    time.sleep(3 * (attempt + 1))
                    continue

                errors.append(f"{pname}: HTTP {e.code} {err_text[:120]}")
                break
            except Exception as e:
                errors.append(f"{pname}: Network/Parsing Error {e}")
                time.sleep(2)

    err_summary = f"所有 LLM 容灾厂商均调用失败或未配置 Key! 追踪信息: {' | '.join(errors)}"
    send_generation_alert(_resolve_book_title(messages), err_summary)
    raise RuntimeError(err_summary)


def call_minimax(prompt, model="MiniMax-M3.1-Flash", max_tokens=8000, timeout=600, thinking_disabled=True, effort="medium", retries=5):
    """调用 MiniMax，带 retry（针对 429/500/502/503/504 + 指数退避）
    2026-10-19 升级到 MiniMax-M3.1-Flash: 采用 output_config.effort 控制。
    2026-10-24 增加 5h 周期限额 (2067) 智能守候机制，平稳跨越重置窗口不丢任务。
    2026-11-11 增加多厂商容灾集成：遭遇 HTTP 402/账单耗尽时熔断 6 小时并自动无缝切入容灾网关。
    """
    import time
    _load_openclaw_env()
    now_ts = time.time()
    if _CIRCUIT_BREAKER.get("MiniMax", 0) > now_ts:
        remain = int(_CIRCUIT_BREAKER["MiniMax"] - now_ts)
        print(f"⚠️ MiniMax 处于 402 计费熔断状态 (剩余 {remain}s)，直接切换至多厂商容灾网关...", file=sys.stderr)
        return call_llm_with_failover(prompt, max_tokens=max_tokens)

    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        print("⚠️ 未检测到 MINIMAX_API_KEY，直接路由至多厂商容灾网关...", file=sys.stderr)
        return call_llm_with_failover(prompt, max_tokens=max_tokens)

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if "M3.1" in model:
        body["output_config"] = {"effort": effort}
    elif thinking_disabled:
        body["thinking"] = {"type": "disabled"}
    last_err = None
    sensitive_count = 0  # 2026-06-09: 跟踪连续 sensitive 次数, 3 次才确定真拒
    for attempt in range(retries + 1):
        try:
            req = request.Request(
                "https://api.minimaxi.com/anthropic/v1/messages",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "x-api-key": key,
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            content = ""
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            # 2026-09-30: MiniMax 对未知模型名会静默回退 M3 (实测: model="NOT-EXIST" → resp.model="MiniMax-M3", HTTP 200, 无 error).
            # 不校验会导致 M3.1 悄悄降级成 M3, 质量无声下降且日志看不出. 必须显式校验.
            resp_model = resp.get("model", "")
            if resp_model and resp_model != model:
                msg = f"模型静默降级: 请求 {model} → 实际返回 {resp_model} (MiniMax 对未知模型名静默回退 M3). 拒绝静默使用."
                print(f"🚨 {msg}", file=sys.stderr, flush=True)
                raise RuntimeError(msg)
            if attempt > 0:
                print(f"✅ 第 {attempt+1} 次重试成功", file=sys.stderr)
            return content
        except error.HTTPError as e:
            last_err = e
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                err_body = str(e.reason)
            if "new_sensitive" in err_body or "sensitive" in err_body.lower():
                # 2026-06-09: M3 sensitive 是启发式, 同样 prompt 多次调用结果不一致
                # 3 次连续 sensitive 才视为真拒 (避免误杀)
                sensitive_count += 1
                if sensitive_count >= 3 or attempt >= retries:
                    # 2026-10-19: M3.1 降级到 M3 (主备冗余), M3 再降级到 M2.7
                    if "M3.1" in model and os.environ.get("MINIMAX_FALLBACK_M3", "1") == "1":
                        print(f"⚠️ M3.1 sensitive x{sensitive_count} — fallback M3 (主备冗余)", file=sys.stderr)
                        return call_minimax(prompt, model="MiniMax-M3", max_tokens=max_tokens, timeout=timeout, thinking_disabled=True, retries=2)
                    elif model == "MiniMax-M3" and os.environ.get("MINIMAX_FALLBACK_M27", "1") == "1":
                        print(f"⚠️ M3 sensitive x{sensitive_count} — fallback M2.7 (主备冗余)", file=sys.stderr)
                        return call_minimax(prompt, model="MiniMax-M2.7", max_tokens=max_tokens, timeout=timeout, thinking_disabled=False, retries=2)
                    print(f"⚠️ 内容审核拒 (sensitive x{sensitive_count}), 确认真拒", file=sys.stderr)
                    raise
                print(f"⚠️ M3 sensitive (第 {sensitive_count} 次, 启发式, 重试中)", file=sys.stderr)
                time.sleep(10)  # 短退避
                continue
            print(f"⚠️ HTTPError {e.code} (attempt {attempt+1}/{retries+1}): {e.reason}", file=sys.stderr)
            is_billing_err = (
                e.code == 402 or
                any(k in err_body.lower() for k in ("billing", "insufficient", "quota", "balance", "exhausted", "overdue", "unpaid")) or
                any(k in err_body for k in ("余额", "欠费", "额度不足", "充值"))
            )
            if is_billing_err:
                _CIRCUIT_BREAKER["MiniMax"] = time.time() + 6 * 3600
                reason_msg = f"MiniMax HTTP {e.code} 余额耗尽/配额触顶 (已触发 6 小时进程熔断)"
                print(f"🚨 {reason_msg}，切入容灾网关...", file=sys.stderr)
                trigger_402_watchdog("MiniMax", reason_msg)
                send_generation_alert(_resolve_book_title(prompt), reason_msg)
                try:
                    return call_llm_with_failover(prompt, max_tokens=max_tokens)
                except Exception as fe:
                    print(f"❌ 容灾切流亦失败: {fe}", file=sys.stderr)
                raise
            # 2026-11-18 强化: 遵循 MiniMax Token Plan 5小时重置窗口 (00:00, 05:00, 10:00, 15:00, 20:00 BJT)
            # 撞 429 严禁反复盲目重试浪费算力与并发，直接对齐至下一个重置点唤醒续产
            if e.code == 429:
                wait_sec = seconds_until_minimax_reset()
                print(f"⏳ [MiniMax 5h重置窗口守护] 检测到 HTTP 429 (Token Plan限流/额度触顶)，停止盲目重试，对齐至下一重置点唤醒 (等待 {wait_sec/60:.1f} 分钟)...", file=sys.stderr, flush=True)
                time.sleep(wait_sec)
                continue
            if e.code in (500, 502, 503, 504):
                if attempt < retries:
                    wait = 8 * (attempt + 1)
                    print(f"   退避 {wait}s 后重试...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                else:
                    print(f"⚠️ MiniMax 重试耗尽 (code={e.code})，尝试容灾切流...", file=sys.stderr)
                    try:
                        return call_llm_with_failover(prompt, max_tokens=max_tokens)
                    except Exception:
                        pass
            raise
        except Exception as e:
            last_err = e
            print(f"⚠️ 异常 (attempt {attempt+1}/{retries+1}): {e}", file=sys.stderr)
            if attempt < retries:
                wait = 5 * (attempt + 1)
                print(f"   退避 {wait}s 后重试...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise last_err


def tts_friendly_preprocess(text):
    """TTS 朗读前预处理（不完美，AI prompt 里也要约束）"""
    # 2026-10-11 兜底清洗: 彻底过滤历史 prompt 词泄露 'M3 偏见' / 'M三偏见' 及生硬标题头 '洞察一：'
    text = re.sub(r"M\s*[3三]\s*偏见", "常见认知偏见", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)^\s*洞察[一二三四五六七八九十\d]+[：:——\-\s]*", "", text)
    text = re.sub(r"(\d{4})年", lambda m: "".join("零一二三四五六七八九"[int(d)] for d in m.group(1)) + "年", text)
    def num_to_cn(n):
        cn_map = "零一二三四五六七八九"
        return "".join(cn_map[int(d)] for d in n)
    text = re.sub(r"(\d+)%", lambda m: f"百分之{num_to_cn(m.group(1))}", text)
    return text


def _repair_unbalanced_quotes_in_json(raw):
    """2026-06-10 v7 修: M3 中文场景会在字符串里输出未转义半角双引号, 导致 JSON 截断。
    v1-v6 都各有 bug (改坏合法引号 / 只修 1 对 / 等等)。
    v7 策略: 用 raw_decode 增量解析, 遇到错误时, 错误点 p 之前那个 `"` 就是 parser
    误当 string 结束的内部引号 — 直接把它改成中文左引号 `“`, 让 parser 继续读。
    简单, 不需要判断上下文, 因为 p - 1 在 parser 视角下一定是 string 误结束的位置。
    """
    import json
    decoder = json.JSONDecoder()
    cur = raw
    for _round in range(500):
        try:
            obj, end = decoder.raw_decode(cur)
            return cur
        except json.JSONDecodeError as e:
            p = e.pos
            if p is None or p >= len(cur):
                return cur
            # p 是 parser 期望分隔符但拿到 string content 的位置, p-1 一定是误结束的 "
            j = p - 1
            if j < 0 or cur[j] != '"':
                return cur
            # 转 j 这个 " 为 “ (中文左引号), parser 会继续读后面直到真正 string 结束
            cur = cur[:j] + '“' + cur[j+1:]
    return cur


def _clean_trailing_commas(s: str) -> str:
    """2026-11-11 fix: 清洗 JSON 对象或数组末尾的多余逗号，例如: {"a": 1, } -> {"a": 1}, [1, 2, ] -> [1, 2]"""
    return re.sub(r",\s*([\]}])", r"\1", s)


def extract_json(content):
    # 2026-06-07 修复: M3 thinking:disabled 可能不彻底, 主动剥掉 <think>...</think> 块
    content = re.sub(r"<think>[\s\S]*?</think>", "", content)
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        raise RuntimeError(f"无 JSON 起点: {content[:1000]}")
    raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # 2026-11-11 fix: 尝试尾逗号清洗 (Trailing Comma Cleaning)
        try:
            return json.loads(_clean_trailing_commas(raw))
        except json.JSONDecodeError:
            pass

        # 2026-06-09 修: M3 中文场景会在字符串里输出未转义半角双引号, 先转为中文引号
        print(f"⚠️ JSON 解析失败 (try repair): {e}", file=sys.stderr)
        try:
            fixed = _repair_unbalanced_quotes_in_json(raw)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                return json.loads(_clean_trailing_commas(fixed))
        except json.JSONDecodeError:
            pass
        for trim_suffix in ["}", "]", "}", "\n", ""]:
            try:
                trimmed = raw.rsplit("\n", 1)[0] if "\n" in raw else raw
                if not trimmed.endswith(trim_suffix):
                    repaired = trimmed + (trim_suffix if trim_suffix else "}")
                else:
                    repaired = trimmed
                repaired = _clean_trailing_commas(repaired)
                quote_count = repaired.count('"') - repaired.count('\\"')
                if quote_count % 2 != 0:
                    repaired += '"'
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"JSON 解析失败 (repair 也败): {e}\n原始: {raw[:500]}")


# ============ 阶段 1: 本书结构 + 分集建议 ============
def build_phase1_prompt(book_title, book_text):
    """输入: 整本书 + 书名 → 输出: book_structure.json 的 prompt

    2026-06-08: 用 .format(book_title=, snippet=) 替换 f-string (Python 3.9 兼容)
    """
    if len(book_text) > 8000:
        # 2026-06-08: 从 30000 减到 5000+3000, M3 prompt 不能超 10000 token
        snippet = book_text[:5000] + "\n\n[... 中间省略 ...]\n\n" + book_text[-3000:]
    else:
        snippet = book_text

    return """
# 节目形态
- 单本多集（不是 1 集讲完一本，是按'自然阅读节奏'分 N 集）
- 每集对应书的一个'块'（可能是 1 章 / 几章合并 / 一个'卷'或'部分'）
- 目标：把一本厚书拆成**听众愿意分 N 次听完**的节奏
- 单集音频 15-30 分钟（按 ~250 字/分钟 计算 = 3700-7500 字）
- 每集 5 段：定位 (5%) + 章节背景 (15%) + 章节核心 (60%) + 评论延伸 (15%) + 收束+下集预告 (5%)

# 分集原则（重要）
- **不按章节硬切**！如果一本 20 章的书，**应该按'主题/弧线'合并相邻章节**，目标是 4-8 集
- 听感 > 完整性：一本 10 万字的书硬切 20 集没人听
- 集数建议范围：薄书 3-5 集，标准书 5-8 集，厚书 8-12 集
- 每集末尾必须留 1 句'下集预告'

# 输入
书名：{book_title}
正文片段（开头 30K 字 + 结尾 5K 字，省略中间）：

```
{snippet}
```

# 输出（严格 JSON）
```json
{{
  "book_title": "书名",
  "book_summary": "全书 200 字摘要（这本书核心讲什么）",
  "core_themes": ["主题 1", "主题 2", "主题 3"],
  "target_audience": "这本书适合谁读（一句话）",
  "episodes_planned": N,
  "episodes": [
    {{
      "episode_no": 1,
      "title": "本集标题（10-20 字）",
      "coverage": "本集覆盖书的哪几章 / 哪些节（用书里的章节名）",
      "core_question": "本集要回答的核心问题（让听众 catch 住）",
      "key_points": ["本集要讲的 3-5 个关键点", "..."]
    }},
    ... 共 N 个
  ]
}}
```

# 重要
- 【只输出 JSON】，不要任何解释
- 集数 N 由你定，按上述原则
- coverage 字段**必须用书里实际的章节名**（方便后续 M3 准确切分）
""".format(book_title=book_title, snippet=snippet)


# ============ 阶段 2: 写第 X 集 ============
def build_phase2_prompt(book_title, book_text, structure, episode_no, prior_summaries):
    """输入: 整本书 + book_structure + 前面集摘要 + 当前集号 → 输出: 第 X 集内容的 prompt

    2026-06-08: 用 .format() 替换 f-string
    2026-06-08: 改为 head + tail 拆段, 防止 M3 单次输出超 8000 token 截断
    """
    ep = next((e for e in structure['episodes'] if e['episode_no'] == episode_no), None)
    if not ep:
        raise ValueError(f"找不到第 {episode_no} 集")

    # 截取与本集相关的章节（简化：传全文，15K 字符限制）
    snippet = book_text[:15000]
    if len(book_text) > 15000:
        snippet += "\n\n[... 中间省略 ...]\n\n" + book_text[-3000:]

    # 前面已写集摘要
    prior_text = ""
    if prior_summaries:
        prior_text = "\n\n".join(
            f"第 {s['episode_no']} 集《{s['title']}》摘要：{s['summary']}"
            for s in prior_summaries
        )
    if not prior_text:
        prior_text = "（这是第 1 集, 前面没有）"
    return """
- 调性：吸引人 + 内容充实；不鸡汤；不文青
- 听众：爱读书、爱思考、想要"听完能用"的人
- 节目名：《好好读书》
- 这是"AI 讲书 + 评论"节目，每集 = 章节解说 + 评论延伸

# 朗读硬约束（TTS 工程，必须严格遵守）
- **数字必须用汉字**：30% → 百分之三十；5GB → 五个 G
- **年份必须拆位**：2026年 → 二零二六年
- **英文术语必须拆字母或拼读**：AI → A I；M3 → M 三；edge-tts → edge tts
- **长句拆短**：每句不超过 50 字
- **避免**：咱们、你猜、对吧、然后呢、嗯、啊 等聊天体
- **不要**：表情符号、Markdown、链接、停顿标签
- **标点控节奏**："," 短停；"。" 长停；"；"中停

# 文字稿节奏（让 TTS 念起来有'人味'，不是'读课文'）
## 长度控制
- **短句 1 句 1 行**：每段 50-80 字最佳，不要写成 200 字的稀拉长段
- **每 3-5 句一个段**：中间用 "。" "；" 切不要用 "，" 堆
- **不要把"中逗号"当"句号"** 念：句号是完整句，逗号是中间顿

## 节奏变化（重要：不要机械感）
- **句间停顿不均**：
  - "，" 后短顿 300ms（不是 600）
  - "。" 后 1000ms
  - "？" 后 1200ms（反问推进）
  - "；" 后 1000ms
  - 段首 1500ms（"深呼吸"）
  - 段末 800ms（不是"听完"）
- **靠句子类型控节奏**，不是靠标点堆
- **语气词**（"呢/嘛/嗯"）用作自然落地、思考填充，不是口水词
- **金句重复**：关键结论重复一次（TTS 会用不同语调，意外增强记忆）

## 情感驱动
- **不要每段都"陈述"**：重要观点前用"为什么？" "问题在哪儿？" "有意思的是" 反问推进
- **段间过渡**：用'接下来' / '问题是' / '但最让人心酸的还不是这个' / '说到这里' 推进
- **不要连续 3 段同句式**：避免 "X 是 Y / X 是 Y / X 是 Y"
- **用"你/我们"取代"读者/听众"**
- **可用"咱们"作为轻松过渡**（但开场后不要用）
- **可以"我想到一个事" 开头**：M3 主播"人设"定位为"认真读书的人"

# 数字日期
- **数字日期**："1978 年" → "一九七八年"（4 位拆位）
- **百分比**："5%" → "百分之五"

# 你的任务
写第 {episode_no} 集《{ep_title}》（覆盖：{coverage}）。
{prior_text}

# 本集信息
- 书名: {book_title}
- 本集标题: {ep_title}
- 本集覆盖: {coverage}
- 本集核心问题: {core_question}
- 本集关键点: {key_points_text}

# 输入 (正文片段)
```
{snippet}
```

# 输出（严格 JSON, 5 段式）
```json
{{
  "episode_no": {episode_no},
  "title": "{ep_title}",
  "sections": {{
    "position": "本集定位 (200-400 字)",
    "background": "章节背景 (600-1100 字)",
    "core": "章节核心内容 (2200-4500 字)",
    "commentary": "评论延伸 (600-1100 字)",
    "closing": "本集收束 + 下集预告 (200-400 字)"
  }},
  "summary": "本集 100 字摘要"
}}
```

# 重要
- 严格按朗读硬约束写，每个数字/英文/年份都转换
- 【只输出 JSON】，不要任何解释
- 字数宁少勿凑（如果某段写不到目标字数，OK，但不要灌水）
""".format(
        book_title=book_title,
        episode_no=episode_no,
        ep_title=ep.get("title", ""),
        coverage=ep.get("coverage", ""),
        core_question=ep.get("core_question", ""),
        key_points_text="、".join(ep.get("key_points", [])),
        prior_text=prior_text,
        snippet=snippet,
    )


def run_phase1(extracted_txt):
    """阶段 1: 生成本书结构 + 分集方案"""
    f = Path(extracted_txt)
    book_title = f.parent.name
    set_current_book_title(book_title)
    text = f.read_text(encoding="utf-8")

    print(f"📖 阶段 1: 生成本书结构 + 分集方案")
    print(f"   书名: {book_title}")
    print(f"   字数: {len(text)}")

    prompt = build_phase1_prompt(book_title, text)
    print(f"🤖 调用 MiniMax-M3.1-Flash...")
    # 2026-06-09: 7 集书的 episodes 列表可能超 32000 token 截断, 失败时用 64000 重试
    try:
        raw = call_minimax(prompt, model="MiniMax-M3.1-Flash", max_tokens=32000, effort="medium")
        data = extract_json(raw)
    except RuntimeError as e:
        if "JSON 解析失败" in str(e):
            print(f"⚠️ JSON 截断/损坏, 用 max_tokens=64000 重试", flush=True)
            raw = call_minimax(prompt, model="MiniMax-M3.1-Flash", max_tokens=64000, effort="medium")
            data = extract_json(raw)
            print(f"  ✓ 64000 token 重试成功", flush=True)
        else:
            raise

    out = f.parent / "book_structure.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 写入: {out}")
    print(f"📚 建议分集数: {data.get('episodes_planned', '?')}")
    for ep in data.get("episodes", []):
        print(f"   第 {ep.get('episode_no', '?')} 集: {ep.get('title', '?')}（覆盖: {ep.get('coverage', '?')}）")
    return out


def run_phase2(extracted_txt, episode_no):
    """阶段 2: 写第 X 集 (单段式, 适合 8000 token 以内)"""
    f = Path(extracted_txt)
    text = f.read_text(encoding="utf-8")
    book_title = f.parent.name
    set_current_book_title(book_title)

    struct_path = f.parent / "book_structure.json"
    if not struct_path.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase 1")
    structure = json.loads(struct_path.read_text(encoding="utf-8"))

    # 读前面集摘要
    prior_summaries = []
    for prev_no in range(1, episode_no):
        prev_file = f.parent / f"episode_{prev_no}.json"
        if prev_file.exists():
            prev = json.loads(prev_file.read_text(encoding="utf-8"))
            prior_summaries.append({
                "episode_no": prev_no,
                "title": prev.get("title", "?"),
                "summary": prev.get("summary", ""),
            })

    prompt = build_phase2_prompt(book_title, text, structure, episode_no, prior_summaries)
    print(f"📝 阶段 2: 写第 {episode_no} 集 (单段)")
    raw = call_minimax(prompt, model="MiniMax-M3.1-Flash", max_tokens=16000, effort="medium")
    data = extract_json(raw)

    out = f.parent / f"episode_{episode_no}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 拼 txt
    sections = data.get("sections", {})
    full_text = "\n\n".join(
        sections.get(k, "") for k in ["position", "background", "core", "commentary", "closing"]
    )
    out_txt = f.parent / f"episode_{episode_no}.txt"
    out_txt.write_text(full_text, encoding="utf-8")
    print(f"💾 写入: {out_txt} ({len(full_text)} 字)")
    return out


def run_phase2_split(extracted_txt, episode_no):
    """阶段 2 拆段版: head (position+background) + tail (core+commentary+closing)
    
    2026-06-08: 防止 M3 单次输出超 8000 token 截断
    """
    f = Path(extracted_txt)
    text = f.read_text(encoding="utf-8")
    book_title = f.parent.name
    set_current_book_title(book_title)

    struct_path = f.parent / "book_structure.json"
    if not struct_path.exists():
        raise FileNotFoundError(f"book_structure.json 不存在, 先跑 phase 1")
    structure = json.loads(struct_path.read_text(encoding="utf-8"))

    # 读前面集摘要
    prior_summaries = []
    for prev_no in range(1, episode_no):
        prev_file = f.parent / f"episode_{prev_no}.json"
        if prev_file.exists():
            prev = json.loads(prev_file.read_text(encoding="utf-8"))
            prior_summaries.append({
                "episode_no": prev_no,
                "title": prev.get("title", "?"),
                "summary": prev.get("summary", ""),
            })

    ep = next((e for e in structure["episodes"] if e["episode_no"] == episode_no), None)
    if not ep:
        raise ValueError(f"找不到第 {episode_no} 集")

    snippet = text[:12000]
    if len(text) > 12000:
        snippet += "\n\n[... 中间省略 ...]\n\n" + text[-3000:]

    prior_text = ""
    if prior_summaries:
        prior_text = "\n\n".join(
            f"第 {s['episode_no']} 集《{s['title']}》摘要: {s['summary']}"
            for s in prior_summaries
        )

    # head prompt (position + background)
    head_prompt = f"""
你是《好好读书》播客的撰稿人。今天要写第 {episode_no} 集的前半段 (position + background).

# 朗读硬约束 (TTS 工程, 严格遵守)
- 数字必须用汉字: 30% → 百分之三十; 5GB → 五个 G
- 年份必须拆位: 2026年 → 二零二六年
- 英文术语必须拆字母或拼读: AI → A I; M3 → M 三; edge-tts → edge tts
- 长句拆短: 每句不超过 50 字
- 避免: 咱们/你猜/对吧/然后呢/嗯/啊 等聊天体
- 不要: 表情/ Markdown/链接/停顿标签
- 标点控节奏: ',' 短停 300ms; '。' 长停 1000ms; '？' 推进 1200ms; '；'中停 1000ms
- 语气词: 可用 '呢/嘛/嗯' 做自然落地 (但别用 '啊/嗯/那个' 等口水词)
- 短句 1 句 1 行, 每段 50-80 字
- 不用聊天体, 不用聊天体

# 文字稿节奏 (让 TTS 念起来有'人味')
- 句间停顿不均: '，' 后 300ms; '。' 后 1000ms; '？' 后 1200ms; '；' 后 1000ms; 段首 1500ms; 段末 800ms
- 段间过渡: '接下来' / '问题是' / '但最让人心酸的还不是这个' / '说到这里'
- 不要连续 3 段同句式
- 重要观点前用 '为什么？' / '问题在哪儿？' / '有意思的是' 反问推进

# 你的任务
写第 {episode_no} 集《{ep.get("title", "")}》的前半段 (position + background).
{prior_text if prior_text else "（这是第 1 集, 前面没有）"}

# 本集信息
- 书名: {book_title}
- 本集标题: {ep.get("title", "")}
- 本集覆盖: {ep.get("coverage", "")}
- 本集核心问题: {ep.get("core_question", "")}
- 本集关键点: {", ".join(ep.get("key_points", []))}

# 输入 (正文片段)
```
{snippet}
```

# 输出 (严格 JSON)
```json
{{
  "episode_no": {episode_no},
  "sections": {{
    "position": "本集定位 (200-400 字)",
    "background": "章节背景 (600-1100 字)"
  }},
  "summary": "本集 100 字摘要 (用于 TG 通知 + 后续集上下文, 写完后补)"
}}
```

# 重要
- 严格按朗读硬约束写, 每个数字/英文/年份都转换
- 【只输出 JSON】, 不要任何解释
- 字数宁少勿凑 (如果某段写不到目标字数, OK, 但不要灌水)
"""
    
    # tail prompt (core + commentary + closing)
    tail_prompt = f"""
继续写第 {episode_no} 集的后半段 (core + commentary + closing).

# 已写完的前段
position: {ep.get("_position", "")}
background: {ep.get("_background", "")}

# 朗读硬约束 (同上)
- 数字/年份/英文必须按前段的同样规则转写
- 段间停顿 + 节奏控制 同上
- 【只输出 JSON】

# 本集信息
- 书名: {book_title}
- 本集标题: {ep.get("title", "")}

# 输出 (严格 JSON)
```json
{{
  "sections": {{
    "core": "章节核心内容 (2200-4500 字, 3-5 个洞察, 故事化叙述, 适当引用 2-3 句原文)",
    "commentary": "评论延伸 (600-1100 字, M3 偏见 + 含义 + 1-2 个今天就能用的行动)",
    "closing": "本集收束 + 下集预告 (200-400 字, 一句金句 + 下一集讲什么)"
  }}
}}
```
"""
    
    print(f"📝 头段: position + background...", file=sys.stderr)
    head_raw = call_minimax(head_prompt, model="MiniMax-M3.1-Flash", max_tokens=16000, effort="medium")
    head_data = extract_json(head_raw)
    
    # 合并
    sections = head_data.get("sections", {})
    ep_data = {
        "episode_no": episode_no,
        "title": ep.get("title", ""),
        "sections": sections,
        "summary": head_data.get("summary", ""),
    }
    
    print(f"📝 尾段: core + commentary + closing...", file=sys.stderr)
    # 把已写的位置传过去 (用 .update 替换 ep 里临时字段)
    ep_with_pos = {**ep, "_position": sections.get("position", ""), "_background": sections.get("background", "")}
    tail_prompt_final = tail_prompt.format(ep=ep_with_pos, book_title=book_title, episode_no=episode_no)
    tail_raw = call_minimax(tail_prompt_final, model="MiniMax-M3.1-Flash", max_tokens=16000, effort="medium")
    tail_data = extract_json(tail_raw)
    ep_data["sections"].update(tail_data.get("sections", {}))
    
    # 写
    out = f.parent / f"episode_{episode_no}.json"
    out.write_text(json.dumps(ep_data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    # 拼 txt
    full_text = "\n\n".join(
        ep_data["sections"].get(k, "") for k in ["position", "background", "core", "commentary", "closing"]
    )
    out_txt = f.parent / f"episode_{episode_no}.txt"
    out_txt.write_text(full_text, encoding="utf-8")
    print(f"💾 写入: {out_txt} ({len(full_text)} 字)")
    return out


def main():
    parser = argparse.ArgumentParser(description="generate.py — M3 生成")
    parser.add_argument("--phase", type=int, required=True, help="1 或 2")
    parser.add_argument("extracted_txt", help="extracted.txt 路径")
    parser.add_argument("--episode", type=int, default=1, help="阶段 2 时: 写第 X 集")
    parser.add_argument("--mode", choices=["single", "split"], default="single", help="阶段 2 拆段模式")
    args = parser.parse_args()
    
    try:
        if args.phase == 1:
            run_phase1(args.extracted_txt)
        elif args.phase == 2:
            if args.mode == "split":
                run_phase2_split(args.extracted_txt, args.episode)
            else:
                run_phase2(args.extracted_txt, args.episode)
        else:
            print(f"❌ 未知 --phase: {args.phase}")
    except Exception as e:
        book_title = Path(args.extracted_txt).parent.name if args.extracted_txt else ""
        send_generation_alert(book_title, f"生成异常中断 (phase {args.phase}): {e}")
        raise


if __name__ == "__main__":
    main()
