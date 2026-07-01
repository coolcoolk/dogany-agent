#!/usr/bin/env python3
"""
SessionStart hook: 직전 세션의 꼬리 몇 턴을 새 세션 컨텍스트로 주입한다.
세션 간 연속성 복구용. __AGENT_LABEL__ 작업공간에서 호출.

stdin(JSON): {session_id, transcript_path, cwd, source, ...}
stdout(JSON): {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "..."}}
출력 없음 = 주입 안 함(직전 세션 없음/대상 아님).
"""
import sys, os, json, glob

PAIRS = 4          # 직전 세션 마지막 user/assistant 쌍 개수
MAX_MSGS = PAIRS * 2
CHAR_CAP = 1000    # 메시지당 길이 컷


def text_of(message):
    """user/assistant 메시지에서 순수 텍스트만. 툴/노이즈는 None."""
    c = message.get("content")
    if isinstance(c, str):
        return c.strip() or None
    if not isinstance(c, list):
        return None
    parts = []
    for b in c:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        # tool_use / tool_result / thinking 등은 스킵
    s = "\n".join(p for p in parts if p).strip()
    return s or None


def turns_of(path):
    """한 transcript 파일에서 user/assistant 텍스트 턴 목록을 시간순으로 뽑는다."""
    turns = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                m = o.get("message", {})
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                t = text_of(m)
                if not t:
                    continue
                # 명령/시스템 잡음 스킵
                low = t.lstrip()
                if low.startswith("<") and ("command-name" in t or "task-notification" in t
                                            or "system-reminder" in t):
                    continue
                if len(t) > CHAR_CAP:
                    t = t[:CHAR_CAP] + " …(생략)"
                turns.append((role, t))
    except Exception:
        return []
    return turns


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if data.get("source") not in ("startup", "clear"):
        return

    tp = data.get("transcript_path") or ""
    if tp and os.path.isfile(tp):
        proj_dir = os.path.dirname(tp)
        cur = os.path.basename(tp)
    else:
        cwd = data.get("cwd") or os.getcwd()
        enc = cwd.replace("/", "-")
        proj_dir = os.path.expanduser(f"~/.claude/projects/{enc}")
        cur = os.path.basename(tp) if tp else None
    if not os.path.isdir(proj_dir):
        return

    files = [f for f in glob.glob(os.path.join(proj_dir, "*.jsonl"))
             if os.path.basename(f) != cur]
    if not files:
        return

    # 직전 세션부터 mtime 역순으로 거슬러 올라가며 MAX_MSGS 만큼 채운다.
    # 각 세션 안은 시간순. 합칠 때 오래된 세션이 위로 오도록 앞에 붙인다.
    files.sort(key=os.path.getmtime, reverse=True)
    turns = []
    for path in files:
        if len(turns) >= MAX_MSGS:
            break
        turns = turns_of(path) + turns

    if not turns:
        return
    tail = turns[-MAX_MSGS:]

    label = {"user": "__USER_LABEL__", "assistant": "나(직전세션)"}
    lines = ["[세션 연속성] 직전 세션의 마지막 대화 꼬리입니다. "
             "이어지는 작업이면 맥락으로 활용하되, __USER_LABEL__이 새 주제를 꺼내면 무시하세요.\n"]
    for role, t in tail:
        lines.append(f"### {label.get(role, role)}\n{t}\n")
    ctx = "\n".join(lines)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
