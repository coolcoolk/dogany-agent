#!/usr/bin/env python3
"""
__AGENT_LABEL__ 장기기억 회상 코어 (memory.py)

- 진실의 원천: ../memories/*.md (마크다운)
- state.db: 언제든 `index`로 재생성 가능한 인덱스 (FTS5 trigram + bge-m3 임베딩 BLOB)
- 하이브리드 검색: FTS5 키워드 순위 + 벡터 코사인 순위를 RRF로 융합

서브커맨드: index / search / stats
임베딩: 로컬 Ollama bge-m3 (http://localhost:11434/api/embeddings)
의존성: 표준 라이브러리만 사용 (numpy 없어도 순수 파이썬 코사인).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request

# ---- 경로 상수 ----
HERE = os.path.dirname(os.path.abspath(__file__))
MEMORIES_DIR = os.path.normpath(os.path.join(HERE, "..", "memories"))
DB_PATH = os.path.join(HERE, "state.db")

# 검색 인덱싱 제외 파일. USER.md는 hot(@import로 CLAUDE.md에 상시주입)이라
# 항상 머리에 있어 검색 인덱싱이 중복 → 제외. (2026-06-25)
INDEX_EXCLUDE = {"USER.md"}

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024  # bge-m3 출력 차원

RRF_K = 60          # RRF 표준 상수
MISS_THRESHOLD = 0.30  # top1 코사인이 이 값 미만이면 "미스"로 집계 (stats)
TRANSCRIPT_FTS_TOPK = 3  # 당일 롤링 인덱스 검색에서 끼워넣을 최대 raw 대화 수


# ======================================================================
# 마크다운 파싱
# ======================================================================
def _is_header(line):
    """마크다운 헤더(#, ##, ###...) 여부."""
    return bool(re.match(r"^#{1,6}\s+\S", line.strip()))


def _header_text(line):
    """헤더 라인에서 # 제거한 텍스트."""
    return re.sub(r"^#{1,6}\s+", "", line.strip()).strip()


def _clean(text):
    """노트 본문 정리: 양끝 공백 제거, 빈 헤더-only 제거."""
    return text.strip()


# 박스드로잉/도형 문자 (ASCII 다이어그램 노이즈). 검색 인덱싱 시점에만 제거.
_BOX_CHARS = set(
    "─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣"
    "┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋"
    "═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬"
    "▀▁▂▃▄▅▆▇█▉▊▋▌▍▎▏▐░▒▓▔▕▖▗▘▙▚▛▜▝▞▟"
)


def _box_ratio(line):
    """라인에서 (박스드로잉+공백) 문자가 차지하는 비율. 비어있으면 0."""
    stripped = line.strip()
    if not stripped:
        return 0.0
    box = sum(1 for ch in stripped if ch in _BOX_CHARS)
    return box / len(stripped)


def denoise_for_index(text):
    """
    인덱싱 시점 전용 노이즈 정제. 원본 .md 파일은 절대 건드리지 않는다.
    - 코드펜스(```) 블록 통째 제거.
    - 박스드로잉 문자 비율이 50% 넘는 라인 제거 (ASCII 박스 다이어그램).
    텍스트 설명이 섞인 라인은 그대로 살린다 (박스만 빠짐).
    반환: 정제된 텍스트(빈 문자열일 수 있음 → 호출부에서 스킵 판단).
    """
    out = []
    in_fence = False
    for ln in text.splitlines():
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            continue  # 펜스 라인 자체도 버림
        if in_fence:
            continue  # 코드펜스 내부 통째 제거
        if _box_ratio(ln) > 0.5:
            continue  # 박스 비율 50% 초과 라인 제거
        out.append(ln)
    return "\n".join(out).strip()


def parse_markdown(path):
    """
    하나의 md 파일을 노트 리스트로 파싱.
    반환: [{"section": str, "text": str}, ...]

    규칙:
    - 직전에 등장한 마크다운 헤더(아무 레벨)를 그 이후 노트들의 section 으로 사용.
    - 파일에 '§' 구분자가 있으면: 헤더 라인을 제외한 본문을 '§' 기준으로 쪼개 각 블록을 노트로.
    - '§'가 없으면(폴백): 헤더 사이 구간을 하나의 후보로 보되,
      너무 길면 빈 줄 기준 문단으로 더 쪼갠다.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        print(f"  [warn] 읽기 실패 {path}: {e}", file=sys.stderr)
        return []

    if not raw.strip():
        return []  # 빈 파일 방어

    lines = raw.splitlines()
    has_sep = any(ln.strip() == "§" for ln in lines)

    notes = []
    current_section = ""

    if has_sep:
        # § 기반 파싱: 헤더는 section 갱신, 나머지는 § 로 블록 분리
        buf = []

        def flush():
            txt = _clean("\n".join(buf))
            if txt and txt != "§":
                notes.append({"section": current_section, "text": txt})
            buf.clear()

        for ln in lines:
            s = ln.strip()
            if s == "§":
                flush()
            elif _is_header(ln):
                # 헤더를 만나면 진행 중 블록 마감 후 section 갱신
                flush()
                current_section = _header_text(ln)
            else:
                buf.append(ln)
        flush()
    else:
        # 폴백: 헤더 구간 → 문단 단위 분리
        section_buf = []

        def flush_section():
            if not section_buf:
                return
            lines_in = section_buf[:]
            section_buf.clear()
            # 불릿 기반 청킹: 최상위 불릿("- ") = 청크 경계. 하위 들여쓰기/평문/빈줄은 현재 청크에 흡수.
            # 불릿이 없으면 기존 폴백(빈 줄 문단 기준)으로 분리.
            if any(re.match(r"^- ", ln) for ln in lines_in):
                cur = []

                def push():
                    t = _clean("\n".join(cur))
                    if t:
                        notes.append({"section": current_section, "text": t})
                    cur.clear()

                for ln in lines_in:
                    if re.match(r"^- ", ln):
                        push()
                    cur.append(ln)
                push()
            else:
                block = _clean("\n".join(lines_in))
                if block:
                    paras = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
                    for p in paras:
                        notes.append({"section": current_section, "text": p})

        for ln in lines:
            if _is_header(ln):
                flush_section()
                current_section = _header_text(ln)
            else:
                section_buf.append(ln)
        flush_section()

    return notes


# ======================================================================
# 임베딩 (Ollama bge-m3)
# ======================================================================
def embed(text):
    """
    bge-m3 임베딩 1건 요청. 실패 시 예외 발생(호출부에서 처리).
    반환: list[float] 길이 EMBED_DIM
    """
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding")
    if not vec or not isinstance(vec, list):
        raise ValueError("Ollama 응답에 embedding 없음")
    return vec


def vec_to_blob(vec):
    """float 리스트 → float32 BLOB."""
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vec(blob):
    """float32 BLOB → float 리스트."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a, b):
    """순수 파이썬 코사인 유사도. (노트 수백 개 규모라 충분)"""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ======================================================================
# DB 스키마
# ======================================================================
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,   -- 파일 경로(상대)
            sha256      TEXT NOT NULL,       -- 내용 해시(증분용)
            indexed_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            section     TEXT,
            text        TEXT NOT NULL,
            embedding   BLOB                 -- float32 BLOB (Ollama 다운 시 NULL 가능)
        );

        CREATE INDEX IF NOT EXISTS idx_notes_file ON notes(source_file);

        -- FTS5 trigram: 한국어 부분일치에 유리
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
        USING fts5(text, content='notes', content_rowid='id', tokenize='trigram');

        CREATE TABLE IF NOT EXISTS search_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            query      TEXT NOT NULL,
            n_results  INTEGER NOT NULL,
            top1_score REAL,
            ts         TEXT NOT NULL
        );

        -- 야간 공고화(consolidate) 워터마크: 어디까지 대화를 처리했는지 기록.
        -- key='last_ts' 에 마지막 처리 메시지의 ISO8601 타임스탬프 저장.
        -- 주간 inbox 분류(classify-inbox) 성공 마커: key='classify_inbox_last'.
        CREATE TABLE IF NOT EXISTS consolidation_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- 당일 롤링 인덱스: 아직 공고화 안 된 오늘 raw 대화를 기계적으로 적재.
        -- LLM 0. 새벽 증류로 정식 노트가 된 뒤 그날치는 프룬한다(consolidate 말미).
        CREATE TABLE IF NOT EXISTS transcript_notes (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,   -- 메시지 ISO8601 타임스탬프
            role    TEXT,            -- __USER_LABEL__ / __AGENT_LABEL__
            session TEXT,            -- 출처 jsonl stem(세션 구분/디버그용)
            text    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tnotes_ts ON transcript_notes(ts);

        CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts
        USING fts5(text, content='transcript_notes', content_rowid='id', tokenize='trigram');
        """
    )
    conn.commit()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ======================================================================
# index 커맨드
# ======================================================================
def cmd_index(args):
    # --lock: 시작 시 락 파일 잡고 atexit으로 해제(자동 재인덱싱 중복 방지).
    #         hook 경로의 _maybe_reindex가 던지는 백그라운드 index가 이 락을 쓴다.
    #         스킬에서 호출하는 `memory.py index`는 --lock 없어 그대로 동작.
    lock_path = getattr(args, "lock", None)
    if lock_path:
        import atexit

        def _release_lock():
            try:
                os.remove(lock_path)
            except OSError:
                pass
        atexit.register(_release_lock)

    if not os.path.isdir(MEMORIES_DIR):
        print(f"[error] memories 디렉토리 없음: {MEMORIES_DIR}", file=sys.stderr)
        return 1

    conn = connect()
    init_db(conn)

    md_files = sorted(
        f for f in os.listdir(MEMORIES_DIR)
        if f.endswith(".md") and f not in INDEX_EXCLUDE
    )
    if not md_files:
        print("[warn] md 파일 없음")
        return 0

    # 기존 해시 로드
    existing = {row["path"]: row["sha256"] for row in conn.execute("SELECT path, sha256 FROM files")}

    # 고아 정리: 디스크에서 사라진 파일(파일분할/병합/삭제)의 노트·FTS·files 레코드 제거.
    # 증분 인덱싱은 현존 파일만 순회하므로 이 단계 없이는 삭제된 파일의 노트가 DB에 고아로 남아
    # 검색에 중복으로 떠버린다(파일분할·잘 때 정리의 분화/병합에 필수).
    disk_set = set(md_files)
    tracked = set(existing) | {
        r["source_file"] for r in conn.execute("SELECT DISTINCT source_file FROM notes")
    }
    for fname in sorted(tracked - disk_set):
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE source_file=?", (fname,)
        )]
        for nid in old_ids:
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (nid,))
        conn.execute("DELETE FROM notes WHERE source_file=?", (fname,))
        conn.execute("DELETE FROM files WHERE path=?", (fname,))
        reason = "인덱싱 제외" if fname in INDEX_EXCLUDE else "디스크에서 사라짐"
        print(f"  - {fname}: {reason} → {len(old_ids)} 노트 정리")
    conn.commit()

    total_notes = 0
    embed_calls = 0
    skipped_files = 0
    ollama_down = False

    for fname in md_files:
        fpath = os.path.join(MEMORIES_DIR, fname)
        sha = file_sha256(fpath)

        if existing.get(fname) == sha:
            # 변경 없음 → 재임베딩/재파싱 스킵
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE source_file=?", (fname,)
            ).fetchone()["c"]
            total_notes += cnt
            skipped_files += 1
            print(f"  = {fname}: 변경 없음, 스킵 ({cnt} 노트)")
            continue

        # 변경됨 → 해당 파일 노트 전부 삭제 후 재적재
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE source_file=?", (fname,)
        )]
        for nid in old_ids:
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (nid,))
        conn.execute("DELETE FROM notes WHERE source_file=?", (fname,))

        notes = parse_markdown(fpath)
        # 인덱싱 노이즈 정제(원본 보존): 코드펜스/박스 다이어그램 제거.
        # 정제 후 본문이 비는 노트(박스만 있던 노트)는 인덱싱 스킵.
        cleaned = []
        for note in notes:
            ctext = denoise_for_index(note["text"])
            if not ctext:
                continue
            cleaned.append({"section": note["section"], "text": ctext})
        dropped = len(notes) - len(cleaned)
        notes = cleaned
        if dropped:
            print(f"    [정제] {fname}: 박스/펜스 노트 {dropped}개 스킵")
        if not notes:
            print(f"  ! {fname}: 노트 0개 (빈 파일/구분자 없음)")
            # 해시는 갱신 (다음에 또 파싱 시도 안 하도록)
            conn.execute(
                "INSERT OR REPLACE INTO files(path, sha256, indexed_at) VALUES(?,?,?)",
                (fname, sha, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            continue

        for note in notes:
            emb_blob = None
            try:
                vec = embed(note["text"])
                emb_blob = vec_to_blob(vec)
                embed_calls += 1
            except (urllib.error.URLError, OSError, ValueError) as e:
                # Ollama 다운 등: 임베딩 NULL 로 저장(FTS 검색은 여전히 동작)
                ollama_down = True
                print(f"    [warn] 임베딩 실패(텍스트 일부 저장): {e}", file=sys.stderr)

            cur = conn.execute(
                "INSERT INTO notes(source_file, section, text, embedding) VALUES(?,?,?,?)",
                (fname, note["section"], note["text"], emb_blob),
            )
            nid = cur.lastrowid
            conn.execute(
                "INSERT INTO notes_fts(rowid, text) VALUES(?,?)", (nid, note["text"])
            )
            total_notes += 1

        conn.execute(
            "INSERT OR REPLACE INTO files(path, sha256, indexed_at) VALUES(?,?,?)",
            (fname, sha, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        print(f"  + {fname}: {len(notes)} 노트 적재")

    conn.commit()
    conn.close()

    print(f"\n[index 완료] 총 {total_notes} 노트 / 임베딩 호출 {embed_calls}회 / 변경없어 스킵된 파일 {skipped_files}개")
    if ollama_down:
        print("[주의] 일부 임베딩 실패 — Ollama 상태 확인 후 다시 index 권장 (해당 파일 다시 수정하거나 db 삭제 후 재인덱스)")
    return 0


# ======================================================================
# search 커맨드
# ======================================================================
def fts_search(conn, query, limit):
    """
    FTS5 trigram 검색. bm25 오름차순(작을수록 관련)으로 정렬된 note id 리스트.
    trigram 은 따옴표로 감싼 구문 매칭이 안전.
    """
    # 특수문자 escape: 큰따옴표로 감싸 phrase 로 처리
    safe = '"' + query.replace('"', '""') + '"'
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(notes_fts) AS rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (safe, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["rowid"] for r in rows]


def vector_search(conn, query, limit):
    """
    쿼리 임베딩 vs 전체 노트 임베딩 코사인. (id, score) 내림차순 리스트.
    Ollama 다운 시 빈 리스트.
    """
    try:
        qvec = embed(query)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[warn] 쿼리 임베딩 실패 — 벡터검색 생략(FTS만 사용): {e}", file=sys.stderr)
        return []

    scored = []
    for row in conn.execute("SELECT id, embedding FROM notes WHERE embedding IS NOT NULL"):
        vec = blob_to_vec(row["embedding"])
        scored.append((row["id"], cosine(qvec, vec)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def rrf_fuse(fts_ids, vec_scored, k=RRF_K):
    """
    Reciprocal Rank Fusion.
    각 리스트에서 순위 r(0-base) → 점수 1/(k+r+1) 누적.
    반환: [(note_id, rrf_score, cos_score_or_None), ...] 내림차순.
    """
    fused = {}
    cos_map = {nid: sc for nid, sc in vec_scored}

    for rank, nid in enumerate(fts_ids):
        fused[nid] = fused.get(nid, 0.0) + 1.0 / (k + rank + 1)
    for rank, (nid, _sc) in enumerate(vec_scored):
        fused[nid] = fused.get(nid, 0.0) + 1.0 / (k + rank + 1)

    out = [(nid, score, cos_map.get(nid)) for nid, score in fused.items()]
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def search_core(query, k=5, log=True):
    """검색 코어 — 결과 리스트만 만들어 반환(stdout에 print 금지).

    cmd_search(스킬)와 cmd_hook(자동주입)이 공유한다.
    반환: [{"id","source_file","section","text","rrf_score","cosine"}, ...]
    log=True면 search_log에 1행 기록(스킬 동작 보존). hook 경로는 log=False.
    """
    conn = connect()
    init_db(conn)

    # 당일 롤링 인덱스: 검색 직전 오늘 raw 대화를 증분 적재(기계적, best effort).
    # 같은 날 다른 세션 대화가 아직 공고화 전이어도 여기서 잡혀 검색에 노출된다.
    index_transcript_fts(conn)

    # 후보를 넉넉히 모은 뒤 융합
    pool = max(k * 4, 20)
    fts_ids = fts_search(conn, query, pool)
    vec_scored = vector_search(conn, query, pool)

    fused = rrf_fuse(fts_ids, vec_scored)[:k]

    # 노트 메타 로드
    results = []
    for nid, rrf_score, cos_score in fused:
        row = conn.execute(
            "SELECT source_file, section, text FROM notes WHERE id=?", (nid,)
        ).fetchone()
        if not row:
            continue
        results.append(
            {
                "id": nid,
                "source_file": row["source_file"],
                "section": row["section"],
                "text": row["text"],
                "rrf_score": round(rrf_score, 5),
                "cosine": round(cos_score, 4) if cos_score is not None else None,
                "kind": "note",
            }
        )

    # 당일 raw 대화 매치를 별도 소스로 끼워넣는다(임베딩 없음 → FTS bm25만, cosine=None).
    # 노트 랭킹을 흔들지 않도록 RRF에 섞지 않고 뒤에 캡으로 덧붙인다.
    for tr in transcript_fts_search(conn, query, TRANSCRIPT_FTS_TOPK):
        results.append(
            {
                "id": f"t{tr['id']}",
                "source_file": f"오늘대화:{tr.get('session', '')[:8]}",
                "section": f"{tr.get('role', '')} {tr.get('ts', '')[:16]}",
                "text": tr["text"],
                "rrf_score": None,
                "cosine": None,
                "kind": "transcript",
            }
        )

    if log:
        # 검색 로그 기록 (top1 점수는 코사인 기준; 벡터검색 미동작 시 None)
        top1 = results[0]["cosine"] if results and results[0]["cosine"] is not None else None
        conn.execute(
            "INSERT INTO search_log(query, n_results, top1_score, ts) VALUES(?,?,?,?)",
            (query, len(results), top1, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    conn.close()
    return results


def cmd_search(args):
    query = args.query
    k = args.k

    results = search_core(query, k=k, log=True)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print(f'"{query}" — 결과 없음.')
        return 0

    print(f'🔎 "{query}" — 상위 {len(results)}개\n')
    for i, r in enumerate(results, 1):
        cos = f"{r['cosine']:.3f}" if r["cosine"] is not None else "n/a"
        sect = r["section"] or "(섹션 없음)"
        print(f"[{i}] {r['source_file']} › {sect}  (rrf={r['rrf_score']:.4f}, cos={cos})")
        # 본문 들여쓰기 출력
        for ln in r["text"].splitlines():
            print(f"    {ln}")
        print()
    return 0


# ======================================================================
# stats 커맨드
# ======================================================================
def cmd_stats(args):
    conn = connect()
    init_db(conn)

    total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    with_emb = conn.execute(
        "SELECT COUNT(*) c FROM notes WHERE embedding IS NOT NULL"
    ).fetchone()["c"]

    print("== 메모리 인덱스 통계 ==")
    print(f"총 노트: {total}  (임베딩 보유 {with_emb} / 누락 {total - with_emb})")

    print("\n파일별 노트 수:")
    for row in conn.execute(
        "SELECT source_file, COUNT(*) c FROM notes GROUP BY source_file ORDER BY c DESC"
    ):
        print(f"  {row['source_file']:<40} {row['c']}")

    # 검색 로그 요약
    log = conn.execute("SELECT COUNT(*) c FROM search_log").fetchone()["c"]
    print(f"\n총 검색 횟수: {log}")
    if log:
        # 미스율: 결과 0 또는 top1 < 임계
        miss = conn.execute(
            "SELECT COUNT(*) c FROM search_log "
            "WHERE n_results=0 OR top1_score IS NULL OR top1_score < ?",
            (MISS_THRESHOLD,),
        ).fetchone()["c"]
        rate = miss / log * 100
        print(f"검색 미스율: {rate:.1f}%  (결과0 또는 top1 cos<{MISS_THRESHOLD}, {miss}/{log})")

        print("\n최근 검색 로그 (최대 10건):")
        for row in conn.execute(
            "SELECT query, n_results, top1_score, ts FROM search_log ORDER BY id DESC LIMIT 10"
        ):
            t1 = f"{row['top1_score']:.3f}" if row["top1_score"] is not None else "n/a"
            print(f"  {row['ts']}  n={row['n_results']:<2} top1={t1}  {row['query']}")

    conn.close()
    return 0


# ======================================================================
# write 커맨드 (인입 압축 적재, OpenHuman 방식)
# ======================================================================
import subprocess  # noqa: E402  (write 전용, 상단 import 군과 분리)

HAIKU_MODEL = "haiku"  # claude CLI 별칭. 동작 확인됨(2026-06-24).

COMPRESS_PROMPT = (
    "다음 내용에서 __USER_LABEL__(사용자)에 대해 장기 기억할 가치가 있는 영속적 사실만 골라, "
    "각각 한 줄짜리 원자적 항목으로 압축해라. 잡담/일시적 내용/추측은 버려라. "
    "출력은 항목당 한 줄, 불릿이나 번호 없이 순수 텍스트 줄로만. "
    "설명이나 머리말을 절대 붙이지 마라. 날짜·출처 같은 메타데이터도 붙이지 마라(사실 내용만). "
    "기억할 가치가 있는 게 하나도 없으면 정확히 NONE 한 단어만 출력해라.\n\n"
    "내용:\n"
)


def compress_with_haiku(raw_text, prompt_prefix=COMPRESS_PROMPT, model=HAIKU_MODEL):
    """
    헤드리스 claude로 원시 텍스트를 영속 사실 원자 항목으로 압축.
    prompt_prefix: 압축 지시 프롬프트(기본 COMPRESS_PROMPT — __USER_LABEL__ 사실만).
                   consolidate는 CONSOLIDATE_PROMPT 를 넘겨 생활 사실까지 포함.
    model: claude CLI 모델 별칭(기본 haiku — write 단건용).
           consolidate는 CONSOLIDATE_MODEL(sonnet)을 넘겨 판단력 보강.
    반환: [한 줄 항목, ...]  (비면 기억할 것 없음)
    실패 시 RuntimeError.
    """
    prompt = prompt_prefix + raw_text
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{model} 호출 실패: {e}")
    if proc.returncode != 0:
        raise RuntimeError(f"{model} 비정상 종료(rc={proc.returncode}): {proc.stderr.strip()}")

    items = []
    for ln in proc.stdout.splitlines():
        s = ln.strip()
        if not s:
            continue
        # 모델이 혹시 붙인 불릿/번호 제거
        s = re.sub(r"^[-*•]\s+", "", s)
        s = re.sub(r"^\d+[.)]\s+", "", s)
        s = s.strip()
        if not s:
            continue
        # "기억할 것 없음" 신호 처리
        if s.upper() == "NONE":
            return []
        items.append(s)
    return items


def append_notes_to_md(path, section, items):
    """
    대상 md 파일에 § 구분으로 항목들을 append.
    - section 지정 시 해당 헤더(### ...) 블록 끝에 삽입, 없으면 헤더 새로 만들어 파일 끝.
    - section 미지정 시 파일 끝에 append.
    원본 파일에 직접 쓴다(진실의 원천).
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()

    # 적재 구분자를 대상 파일 형식에 맞춘다(청킹 깨짐 방지).
    # parse_markdown 의 has_sep 판정(any(line.strip()=='§'))과 정확히 일치시켜야
    # 양쪽 파서가 같은 모드로 본다:
    #   - § 기반 파일(__AGENT_LABEL__ 주제파일/inbox): 기존대로 "§\n{item}".
    #   - 불릿 기반 파일: "- {item}" (§ 섞으면 has_sep=True 로 뒤집혀
    #     헤더 단위로 뭉쳐 청킹이 깨진다).
    has_sep = any(ln.strip() == "§" for ln in lines)
    if has_sep:
        block = "\n".join(f"§\n{it}" for it in items)
    else:
        block = "\n".join(f"- {it}" for it in items)

    if section:
        # 해당 헤더 라인 인덱스 찾기
        hdr_idx = None
        for i, ln in enumerate(lines):
            if _is_header(ln) and _header_text(ln) == section:
                hdr_idx = i
                break
        if hdr_idx is None:
            # 섹션 없음 → 파일 끝에 새 헤더 + 블록
            tail = content.rstrip("\n")
            new = f"{tail}\n\n### {section}\n{block}\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            return f"새 섹션 '### {section}' 생성 후 파일 끝에 적재"
        # 다음 헤더 직전까지가 이 섹션의 범위
        end_idx = len(lines)
        for j in range(hdr_idx + 1, len(lines)):
            if _is_header(lines[j]):
                end_idx = j
                break
        # 섹션 본문 끝(end_idx 직전)의 후행 빈 줄을 건너뛰어 삽입 위치 결정
        insert_at = end_idx
        while insert_at - 1 > hdr_idx and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines = lines[:insert_at] + block.splitlines() + lines[insert_at:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
        return f"섹션 '### {section}' 끝에 적재"
    else:
        tail = content.rstrip("\n")
        new = f"{tail}\n{block}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        return "파일 끝에 적재"


def cmd_write(args):
    # 입력: 인자 우선, 없으면 stdin
    if args.text:
        raw = args.text
    else:
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        print("[error] 입력 텍스트가 비어있다.", file=sys.stderr)
        return 1

    # 1) 압축
    try:
        items = compress_with_haiku(raw)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    if not items:
        print("기억할 만한 영속 사실이 없어 적재하지 않음.")
        return 0

    # 2) 메타 부착: (날짜, source)
    today = datetime.date.today().isoformat()
    src = args.source or "출처미상"
    tagged = [f"{it} ({today}, {src})" for it in items]

    # 대상 파일 경로
    target = os.path.join(MEMORIES_DIR, args.file)

    # 적재 미리보기 출력
    print(f"== 압축 결과 ({len(tagged)}개 항목) → {args.file}" +
          (f" › ### {args.section}" if args.section else " (파일 끝)") + " ==")
    for it in tagged:
        print(f"  § {it}")

    if args.dry_run:
        print("\n[dry-run] 파일 미수정. 위 항목들이 적재될 예정.")
        return 0

    if not os.path.isfile(target):
        print(f"[error] 대상 파일 없음: {target}", file=sys.stderr)
        return 1

    # 3) 파일에 append
    where = append_notes_to_md(target, args.section, tagged)
    print(f"\n[적재 완료] {where}")

    # 4) 인덱스 갱신
    print("[index] 인덱스 갱신 중...")
    rc = cmd_index(argparse.Namespace())
    return rc


# ======================================================================
# consolidate 커맨드 (야간 공고화: 대화 트랜스크립트 → 장기기억 증류)
# ======================================================================
# 핵심: __USER_LABEL__↔__AGENT_LABEL__ 원본 chat log(jsonl)에서 영속 사실을 증류해 inbox.md 로 내린다.
# 워터마크(consolidation_state.last_ts)로 어디까지 처리했는지 기록 → 증분만 본다.
# 야간은 주제 라우팅 없이 무조건 inbox.md 로만 단순 적재(주간 classify-inbox 가 분배).

# 트랜스크립트 위치(Claude Code 프로젝트 로그). __AGENT_LABEL__ 작업공간 세션들.
TRANSCRIPT_GLOB = os.path.join(
    os.path.expanduser("~/.claude/projects"),
    os.path.normpath(os.path.join(HERE, "..")).replace("/", "-"),
    "*.jsonl",
)
TARGET_MEMORY_MD = os.path.join(MEMORIES_DIR, "inbox.md")
DEDUP_THRESHOLD = 0.82     # 후보가 기존 노트와 이 코사인 이상이면 "이미 아는 것"
DEFAULT_LOOKBACK_DAYS = 3  # 워터마크 없을 때(최초) 최근 며칠치를 볼지
MEMORY_LINES_CAP = 600     # inbox.md 줄 수가 이 값 넘으면 "정리할까요?" 제안
CONSOLIDATE_REPORT_ENABLED = os.environ.get("DOGANY_MEMORY_REPORT", "0") == "1"  # product: nightly "memory saved" Telegram push OFF by default
# 대화가 길면 입력 한도를 넘어 압축이 통째로 실패한다.
# 줄(메시지) 경계로 이 글자수 이하 청크로 쪼개 각각 압축한 뒤 항목을 합친다.
CONSOLIDATE_CHUNK_CHARS = 24000
# consolidate는 Sonnet(발화 vs 사실 판단력). 야간 1회라 비용 감당. write는 Haiku 유지.
CONSOLIDATE_MODEL = "sonnet"
# taxonomy 문서 경로(2차 KEEP/DROP 필터가 전문을 주입).
TAXONOMY_PATH = os.path.join(HERE, "CONSOLIDATION_TAXONOMY.md")
# 2차 필터 묶음 호출 시 한 번에 판정할 후보 개수 상한(프롬프트 비대 방지).
FILTER_BATCH_SIZE = 40

# __AGENT_LABEL__(생활비서)용 압축 프롬프트. __USER_LABEL__ 사실 + 생활 결정/습관/관계/일정/가계부도 포함.
# 핵심: 발화 자체·진행보고를 사실로 둔갑시키지 말 것. 확정된 영속 사실/결정만.
CONSOLIDATE_PROMPT = (
    "아래는 __USER_LABEL__(사용자)과 생활비서 에이전트 __AGENT_LABEL__(어시스턴트)가 주고받은 대화 기록이다. "
    "이 안에서 장기 기억할 가치가 있는 영속적 사실/결정만 골라 각각 한 줄짜리 원자적 항목으로 압축해라.\n"
    "\n"
    "절대 원칙: 발화 자체나 진행 상황 보고가 아니라, 대화를 통해 확정된 영속적 사실/결정만 뽑는다. "
    "__AGENT_LABEL__(어시스턴트)가 한 말·질문·보고·멘트는 그 자체로는 사실이 아니다. 그 대화로 무엇이 확정됐는지만 남겨라.\n"
    "\n"
    "반드시 버려라(항목으로 절대 만들지 마라):\n"
    "- 코드블록·코드펜스(``` 류)·명령줄·셸 명령(launchctl, git, python ... 같은 실행 명령)\n"
    "- 질문(예: ~할까요? ~될까요?), 사과(죄송), 인사, 맞장구, 감탄\n"
    "- 발화 라벨(__USER_LABEL__:, __AGENT_LABEL__: 등)이나 그 일부\n"
    "- 테스트/디버깅 진행 멘트(예: 지금 ~해보겠습니다, 이제 ~하는 중, ~확인해보겠습니다)\n"
    "- 한 번 쓰고 마는 일회성 지시·요청, 곧 무의미해질 임시 진행상황(예: ~를 기다리는 중, 아직 ~필요)\n"
    "\n"
    "남겨라(이런 것만):\n"
    "- __USER_LABEL__ 프로필·선호·습관·일/배경\n"
    "- 건강 지표·신체 스탯·목표(칼로리·단백질 모델 등)\n"
    "- 생활 루틴·습관(식단/운동 패턴, 일정 규칙), 관계·사람 맥락, 취향, 재정 패턴\n"
    "- 확정된 결정·합의, 인프라/경로/설정의 영구적 변경\n"
    "- 앞으로도 유효한 규칙·수치(DB ID, 임계값 등)\n"
    "\n"
    "기존에 알던 사실을 뒤집는 정정이면 그 항목 맨 앞에 '정정: ' 을 붙여라.\n"
    "\n"
    "예시(입력 → 출력):\n"
    "- 입력: \"__AGENT_LABEL__: 회고 시간 21시로 옮길까요?\" → 출력: 없음(버림. 질문이라 사실 아님)\n"
    "- 입력: \"__AGENT_LABEL__: 이제 식단 집계를 돌려보겠습니다\" → 출력: 없음(버림. 진행 멘트)\n"
    "- 입력: \"__USER_LABEL__: 단백질 목표 하루 160g으로 잡아\" → 출력: 단백질 목표 하루 160g\n"
    "- 입력: \"__USER_LABEL__, 죄송합니다. 다시 볼게요\" → 출력: 없음(버림. 사과+멘트)\n"
    "(이 대화 청크가 통째로 인사·질문·진행보고·명령뿐이라 남길 사실이 없으면 정확히 NONE 한 단어만 출력)\n"
    "\n"
    "출력은 항목당 한 줄, 불릿이나 번호 없이 순수 텍스트 줄로만. "
    "설명·머리말·발화 라벨을 절대 붙이지 마라. 날짜·출처 같은 메타데이터도 붙이지 마라(사실 내용만).\n"
    "\n"
    "대화 기록:\n"
)

# 트랜스크립트에서 걸러낼 잡음 패턴(hook 주입/시스템 리마인더 등).
_NOISE_MARKERS = (
    "<system-reminder>",
    "[관련 기억 — 자동검색]",
    "[관련 기억",
    "hookSpecificOutput",
    "additionalContext",
)


def _ts_load_watermark(conn):
    """consolidation_state 에서 last_ts(ISO8601) 로드. 없으면 None."""
    row = conn.execute(
        "SELECT value FROM consolidation_state WHERE key='last_ts'"
    ).fetchone()
    return row["value"] if row else None


def _ts_save_watermark(conn, ts_iso):
    """last_ts 워터마크 저장(upsert)."""
    conn.execute(
        "INSERT INTO consolidation_state(key, value) VALUES('last_ts', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (ts_iso,),
    )
    conn.commit()


def _is_noise(text):
    """hook 주입/시스템 리마인더로 보이는 잡음이면 True."""
    if not text:
        return True
    for m in _NOISE_MARKERS:
        if m in text:
            return True
    return False


# 셸 명령으로 보이는 라인 휴리스틱. 흔한 명령어로 시작하면 압축 입력에서 제거.
# (denoise_for_index 가 코드펜스 ```...``` 블록을 통째로 제거하므로, 펜스 밖에
#  맨몸으로 떨어진 명령줄만 추가로 잡으면 된다.)
_SHELL_CMD_HEADS = (
    "sudo ", "launchctl ", "git ", "python ", "python3 ", "pip ", "pip3 ",
    "npm ", "node ", "brew ", "curl ", "wget ", "ssh ", "scp ", "rsync ",
    "cd ", "ls ", "cat ", "rm ", "mv ", "cp ", "mkdir ", "chmod ", "chown ",
    "kill ", "pkill ", "ps ", "grep ", "sed ", "awk ", "tail ", "head ",
    "echo ", "export ", "source ", "bash ", "sh ", "./", "trash ",
    "docker ", "systemctl ", "plutil ", "defaults ",
)

# 알맹이 없는 라벨 줄(콜론 라벨 한 토막). 이걸로 시작하고 뒤에 내용 거의 없으면 제거.
_LABEL_HEADS = (
    "요구사항", "참고", "작업 순서", "다음 할 일", "다음 단계", "현재 상황",
    "현재 상태", "남은 작업", "완료된 것", "할 일", "todo", "선택지", "옵션",
    "정리", "요약", "메모", "주의", "결론", "현황", "진행 상황", "체크리스트",
)
# 진행보고/상태기호. 줄 맨 앞에 오면 제거.
_STATUS_SYMBOLS = ("✓", "✅", "⏳", "🔵", "🟢", "🟡", "⚪", "☑", "✔", "❌", "▶", "▷", "□", "■")


def _is_junk_line(line):
    """압축 입력/후보에서 결정론적으로 버릴 라인인가? (taxonomy DROP 규칙의 코드판)
    - 마크다운 헤더(#), 구분선(---/===), 수평선
    - 셸 명령줄
    - 상태기호로 시작하는 진행보고 줄
    - 물음표로 끝나는 질문 줄
    - '죄송'·'__USER_LABEL__,' 으로 시작하는 사과/호명 줄
    - 알맹이 없는 라벨 줄(요구사항:, 참고: 등 콜론 라벨 한 토막)
    빈 줄은 junk 아님(False) — 흐름 보존용.
    """
    s = line.strip()
    if not s:
        return False
    # 마크다운 헤더
    if re.match(r"^#{1,6}\s+", s):
        return True
    # 구분선/수평선 (---, ===, ___, *** 류 3자 이상)
    if re.match(r"^([-=_*~])\1{2,}\s*$", s):
        return True
    if re.match(r"^[-=_*]{3,}$", s):
        return True
    # 셸 명령줄
    if s.startswith(_SHELL_CMD_HEADS):
        return True
    # 상태기호로 시작하는 진행보고
    if s[0] in _STATUS_SYMBOLS:
        return True
    # 질문(물음표로 끝남)
    if s.endswith("?") or s.endswith("？"):
        return True
    # 사과/호명 시작
    if s.startswith("죄송") or s.startswith("__USER_LABEL__,") or s.startswith("__USER_LABEL__ ,"):
        return True
    # 알맹이 없는 라벨 줄: "라벨:" 또는 "라벨: 한두글자"
    m = re.match(r"^[*\-•\d.\)\s]*([^:：]{1,12})[:：]\s*(.*)$", s)
    if m:
        label = m.group(1).strip().lower()
        rest = m.group(2).strip()
        if label in _LABEL_HEADS and len(rest) < 8:
            return True
    return False


def _strip_junk_lines(text):
    """압축 입력 전처리: _is_junk_line 에 걸리는 라인 제거(원본은 안 건드림).
    코드펜스/박스는 denoise_for_index 가 이미 처리하므로 여기선 그 외 형식구조물·발화·명령."""
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            out.append(ln)
            continue
        if _is_junk_line(ln):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _preprocess_for_compress(text):
    """압축 LLM 입력 전용 정제(원본 트랜스크립트 불변).
    1) denoise_for_index: 코드펜스(```) 블록·박스 다이어그램 제거.
    2) _strip_junk_lines: 헤더·구분선·셸 명령·진행보고·질문·라벨 줄 제거.
    반환: 정제 텍스트(빈 문자열 가능)."""
    return _strip_junk_lines(denoise_for_index(text))


def _rule_filter_candidates(items):
    """압축 후 후처리(이중 안전망): 후보 항목 중 형식구조물/발화/명령이 그대로
    항목이 된 것을 결정론적으로 제거. 발화 라벨 접두('__AGENT_LABEL__:', '__USER_LABEL__:')도 떼어낸다.
    반환: (살아남은 항목 리스트, 버려진 (항목, 사유) 리스트)."""
    kept = []
    dropped = []
    for it in items:
        s = it.strip()
        # 발화 라벨 접두 제거(__USER_LABEL__:, __AGENT_LABEL__:)
        s = re.sub(r"^(__USER_LABEL__|__AGENT_LABEL__)\s*[:：]\s*", "", s).strip()
        if not s:
            dropped.append((it, "빈 항목"))
            continue
        if _is_junk_line(s):
            dropped.append((it, "룰필터(형식/발화/명령)"))
            continue
        # 코드펜스 잔재 단독 항목
        if s.startswith("```") or s == "§":
            dropped.append((it, "코드펜스/구분자"))
            continue
        kept.append(s)
    return kept, dropped


def _extract_text_from_content(content):
    """user/assistant message.content → 사람이 쓴 텍스트만 추출.
    - 문자열이면 그대로(잡음이면 빈 문자열).
    - 리스트면 type=='text' 블록의 text 만(thinking/tool_use/tool_result 버림).
    추출 후 압축 입력용 정제(코드펜스/셸 명령 제거)를 적용한다(원본 불변).
    반환: 정제된 텍스트(빈 문자열일 수 있음)."""
    if isinstance(content, str):
        raw = "" if _is_noise(content) else content.strip()
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t and not _is_noise(t):
                    parts.append(t.strip())
        raw = "\n".join(parts).strip()
    else:
        return ""
    if not raw:
        return ""
    return _preprocess_for_compress(raw)


def _iter_transcript_rows(watermark_iso, now):
    """워터마크 이후의 __USER_LABEL__↔__AGENT_LABEL__ 메시지를 (ts, speaker, text, session) 리스트로 수집.
    collect_transcript(consolidate)와 index_transcript_fts(당일 인덱스)가 공유한다.
    watermark_iso: ISO8601 문자열 또는 None(최초 → now-DEFAULT_LOOKBACK_DAYS).
    반환: timestamp 오름차순 정렬된 리스트(빈 리스트 가능)."""
    if watermark_iso:
        cutoff = watermark_iso
    else:
        cutoff = (now - datetime.timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    import glob

    rows = []  # (ts, speaker, text, session)
    for fp in glob.glob(TRANSCRIPT_GLOB):
        session = os.path.splitext(os.path.basename(fp))[0]
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    typ = o.get("type")
                    if typ not in ("user", "assistant"):
                        continue
                    ts = o.get("timestamp")
                    if not ts or ts <= cutoff:
                        continue  # 워터마크 이하(이미 처리)는 스킵
                    content = o.get("message", {}).get("content")
                    text = _extract_text_from_content(content)
                    if not text:
                        continue
                    speaker = "__USER_LABEL__" if typ == "user" else "__AGENT_LABEL__"
                    rows.append((ts, speaker, text, session))
        except OSError:
            continue

    rows.sort(key=lambda r: r[0])  # timestamp 오름차순
    return rows


def collect_transcript(watermark_iso, now):
    """워터마크 이후의 __USER_LABEL__↔__AGENT_LABEL__ 대화를 시간순으로 수집(consolidate 입력).
    반환: (대화텍스트 str, 처리한 메시지들의 최대 timestamp str 또는 None, 메시지 건수 int).
    """
    rows = _iter_transcript_rows(watermark_iso, now)
    if not rows:
        return "", None, 0
    max_ts = rows[-1][0]
    convo = "\n".join(f"{spk}: {txt}" for _ts, spk, txt, _sess in rows)
    return convo, max_ts, len(rows)


# ======================================================================
# 당일 롤링 인덱스 — transcript_fts 증분 적재/검색/프룬
# ======================================================================
def _tsfts_load_watermark(conn):
    """당일 인덱스 워터마크(ts_fts_last) 로드. 없으면 None."""
    row = conn.execute(
        "SELECT value FROM consolidation_state WHERE key='ts_fts_last'"
    ).fetchone()
    return row["value"] if row else None


def _tsfts_save_watermark(conn, ts_iso):
    """당일 인덱스 워터마크(ts_fts_last) 저장(upsert). consolidate의 last_ts와 별개 키."""
    conn.execute(
        "INSERT INTO consolidation_state(key, value) VALUES('ts_fts_last', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (ts_iso,),
    )


def index_transcript_fts(conn, now=None):
    """오늘 raw 대화를 transcript_notes/transcript_fts에 증분 적재(기계적, LLM 0).
    워터마크(ts_fts_last) 이후 메시지만 추가. 최초 실행이면 오늘 0시(로컬)부터.
    hook/search 경로에서 매번 호출돼도 워터마크 덕에 싸다. 어떤 DB 에러도 삼켜
    검색/턴을 막지 않는다(best effort). 반환: 새로 적재한 행 수."""
    if now is None:
        now = datetime.datetime.now().astimezone()
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        wm = _tsfts_load_watermark(conn)
        if not wm:
            # 최초: 오늘 0시(로컬) 이후만 — 당일 롤링 인덱스 취지.
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            wm = start.astimezone(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        rows = _iter_transcript_rows(wm, now)
        if not rows:
            return 0
        cur = conn.cursor()
        for ts, speaker, text, session in rows:
            cur.execute(
                "INSERT INTO transcript_notes(ts, role, session, text) VALUES(?,?,?,?)",
                (ts, speaker, session, text),
            )
            cur.execute(
                "INSERT INTO transcript_fts(rowid, text) VALUES(?,?)",
                (cur.lastrowid, text),
            )
        _tsfts_save_watermark(conn, rows[-1][0])
        conn.commit()
        return len(rows)
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0


def transcript_fts_search(conn, query, limit):
    """당일 transcript_fts FTS5 trigram 검색(bm25 오름차순). 임베딩 없음 → FTS 전용.
    반환: [{"id","ts","role","session","text"}, ...] 최대 limit."""
    safe = '"' + query.replace('"', '""') + '"'
    try:
        rows = conn.execute(
            "SELECT t.id AS id, t.ts AS ts, t.role AS role, "
            "t.session AS session, t.text AS text "
            "FROM transcript_fts f JOIN transcript_notes t ON t.id=f.rowid "
            "WHERE transcript_fts MATCH ? ORDER BY bm25(transcript_fts) LIMIT ?",
            (safe, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def prune_transcript_fts(conn, cutoff_iso):
    """cutoff_iso 이하(이미 증류돼 정식 노트가 된) 당일 raw 행 삭제. consolidate 말미 호출.
    외부콘텐츠 FTS라 notes_fts 패턴대로 fts→notes 순으로 rowid 삭제. 반환: 삭제 행 수."""
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM transcript_notes WHERE ts <= ?", (cutoff_iso,)
            )
        ]
        for tid in ids:
            conn.execute("DELETE FROM transcript_fts WHERE rowid=?", (tid,))
        conn.execute("DELETE FROM transcript_notes WHERE ts <= ?", (cutoff_iso,))
        conn.commit()
        return len(ids)
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0


# ======================================================================
# raw transcript archive (DGN-093) -- own the raw substrate for future replay
# ======================================================================
# consolidate distills text -> inbox.md then advances the watermark and prunes
# the transcript. Before that discard, append the consumed span (TEXT ONLY) to a
# gzip monthly archive so the raw messages we would otherwise lose are ours.
# Additive + non-destructive: this only reads the consumed rows and writes to
# MEMORIES_DIR/_raw/; it never touches the source jsonl, inbox.md, or the DB.
RAW_ARCHIVE_DIR = os.path.join(MEMORIES_DIR, "_raw")
RAW_ARCHIVE_RETENTION_DAYS = 365  # prune _raw/*.jsonl.gz older than ~1 year

# Strip large base64/data-URI blobs that may be inlined in a text block
# (defense in depth -- _extract_text_from_content already drops image/tool
# blocks, but an agent can paste a data URI or a long base64 run into plain text).
_DATA_URI_RE = re.compile(r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+")
_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")


def _scrub_binary_blobs(text):
    """Remove inline base64 / data-URI payloads from a text string.
    Keeps human/agent prose; replaces stripped blobs with a short placeholder."""
    if not text:
        return ""
    t = _DATA_URI_RE.sub("[image]", text)
    t = _LONG_B64_RE.sub("[blob]", t)
    return t.strip()


def archive_raw_transcript(watermark_iso, now):
    """Append the consumed transcript span (TEXT ONLY) to a gzip monthly archive.
    Uses the SAME source/args as collect_transcript for this watermark span
    (_iter_transcript_rows) -- it does not read beyond the consumed span.
    One JSON line per message: {ts, role, text}. Messages with no text after
    scrubbing (pure image/tool) are skipped. Best effort: any error is swallowed
    so archiving never blocks consolidate. Returns count of lines written."""
    import gzip

    rows = _iter_transcript_rows(watermark_iso, now)
    if not rows:
        return 0
    try:
        os.makedirs(RAW_ARCHIVE_DIR, exist_ok=True)
    except OSError as e:
        print(f"[warn] raw archive dir 생성 실패(스킵): {e}", file=sys.stderr)
        return 0

    written = 0
    try:
        # Group by month (YYYY-MM of the message ts) so a span crossing a month
        # boundary lands in the right monthly file.
        for ts, role, text, _session in rows:
            clean = _scrub_binary_blobs(text)
            if not clean:
                continue  # pure image/tool message -> skip (TEXT ONLY)
            month = (ts or "")[:7]  # ISO8601 -> YYYY-MM
            if not re.match(r"^\d{4}-\d{2}$", month):
                month = now.strftime("%Y-%m")  # fallback for malformed ts
            path = os.path.join(RAW_ARCHIVE_DIR, f"{month}.jsonl.gz")
            line = json.dumps(
                {"ts": ts, "role": role, "text": clean}, ensure_ascii=False
            )
            with gzip.open(path, "at", encoding="utf-8") as gz:
                gz.write(line + "\n")
            written += 1
    except OSError as e:
        print(f"[warn] raw archive 쓰기 실패(부분 저장 가능): {e}", file=sys.stderr)
    return written


def prune_raw_archive(now=None):
    """Delete _raw/*.jsonl.gz older than RAW_ARCHIVE_RETENTION_DAYS.
    Age is judged by the YYYY-MM in the filename (mtime fallback). Best effort.
    Returns count of files removed."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if not os.path.isdir(RAW_ARCHIVE_DIR):
        return 0
    cutoff = now - datetime.timedelta(days=RAW_ARCHIVE_RETENTION_DAYS)
    removed = 0
    for fname in os.listdir(RAW_ARCHIVE_DIR):
        if not fname.endswith(".jsonl.gz"):
            continue
        path = os.path.join(RAW_ARCHIVE_DIR, fname)
        m = re.match(r"^(\d{4})-(\d{2})\.jsonl\.gz$", fname)
        too_old = False
        if m:
            # Compare end-of-month against cutoff so a whole month is only pruned
            # once every day in it is past retention.
            year, mon = int(m.group(1)), int(m.group(2))
            if mon == 12:
                nxt = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
            else:
                nxt = datetime.datetime(year, mon + 1, 1, tzinfo=datetime.timezone.utc)
            too_old = nxt <= cutoff
        else:
            # Non-standard name -> fall back to mtime.
            try:
                mtime = datetime.datetime.fromtimestamp(
                    os.path.getmtime(path), datetime.timezone.utc
                )
                too_old = mtime < cutoff
            except OSError:
                too_old = False
        if too_old:
            try:
                os.remove(path)
                removed += 1
                print(f"[consolidate] raw archive 만료 삭제: {fname}")
            except OSError as e:
                print(f"[warn] raw archive 삭제 실패 {fname}: {e}", file=sys.stderr)
    return removed


def _chunk_convo(convo, max_chars=CONSOLIDATE_CHUNK_CHARS):
    """대화 텍스트를 줄(메시지) 경계로 max_chars 이하 청크 리스트로 분할.
    단일 줄이 max_chars 보다 길면 그 줄은 단독 청크(어쩔 수 없이 글자수로 잘림)."""
    chunks = []
    cur = []
    cur_len = 0
    for ln in convo.split("\n"):
        add = len(ln) + 1
        if cur and cur_len + add > max_chars:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(ln)
        cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def compress_convo_chunked(convo):
    """긴 대화를 청크로 나눠 각각 CONSOLIDATE_PROMPT(Sonnet) 로 1차 압축.
    1차는 느슨하게 많이 뽑고(재현율 우선), 정밀도는 룰필터+2차 필터가 책임진다.
    청크 하나가 실패하면 그 청크만 건너뛰고 계속(부분 실패 허용).
    반환: (항목 리스트, 실패한 청크 수)."""
    chunks = _chunk_convo(convo)
    all_items = []
    failed = 0
    seen = set()
    for i, ch in enumerate(chunks, 1):
        try:
            items = compress_with_haiku(
                ch, prompt_prefix=CONSOLIDATE_PROMPT, model=CONSOLIDATE_MODEL
            )
        except RuntimeError as e:
            failed += 1
            print(f"  [warn] 청크 {i}/{len(chunks)} 압축 실패(스킵): {e}", file=sys.stderr)
            continue
        for it in items:
            key = it.strip().lower()
            if key and key not in seen:
                seen.add(key)
                all_items.append(it)
    print(f"[consolidate] 청크 {len(chunks)}개 중 {len(chunks) - failed}개 압축 성공")
    return all_items, failed


def _load_taxonomy():
    """CONSOLIDATION_TAXONOMY.md 전문 로드. 없으면 빈 문자열(2차 필터는 그래도 동작)."""
    try:
        with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        print(f"[warn] taxonomy 문서 없음: {TAXONOMY_PATH}", file=sys.stderr)
        return ""


def _parse_keepdrop(stdout, n):
    """2차 필터 출력 파싱. 기대 형식: 줄당 '<번호> KEEP' 또는 '<번호> DROP <사유>'.
    반환: {idx(1-base): (verdict 'KEEP'|'DROP', reason)}.
    파싱 못한 번호는 호출부에서 보수적으로 DROP 처리(누락=DROP)."""
    verdicts = {}
    for ln in stdout.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^\[?(\d+)\]?[).:\s-]+\s*(KEEP|DROP)\b\s*(.*)$", s, re.IGNORECASE)
        if not m:
            continue
        idx = int(m.group(1))
        verdict = m.group(2).upper()
        reason = m.group(3).strip().lstrip("-—:").strip()
        if 1 <= idx <= n:
            verdicts[idx] = (verdict, reason or ("" if verdict == "KEEP" else "사유없음"))
    return verdicts


def _second_stage_filter(candidates):
    """2차 KEEP/DROP 필터(Sonnet). taxonomy 기준으로 각 후보를 최종 판정.
    후보를 FILTER_BATCH_SIZE 묶음으로 번호 매겨 한 번에 판정(개별 호출 X).
    반환: (kept 리스트, dropped (항목,사유) 리스트, 필터실패여부 bool).
    필터 호출이 실패하면 보수 폴백: 그 묶음은 전부 KEEP(놓침 방지)하고 실패 플래그."""
    if not candidates:
        return [], [], False

    taxonomy = _load_taxonomy()
    kept = []
    dropped = []
    filter_failed = False

    for start in range(0, len(candidates), FILTER_BATCH_SIZE):
        batch = candidates[start:start + FILTER_BATCH_SIZE]
        numbered = "\n".join(f"{i}. {it}" for i, it in enumerate(batch, 1))
        prompt = (
            "너는 장기기억 공고화의 최종 게이트키퍼다. 아래 분류 기준 문서에 따라 "
            "각 후보 항목을 KEEP(남김) 또는 DROP(버림)으로 판정해라.\n"
            "한 달~일 년 뒤에도 참이고 그때 검색해 꺼내 쓸 가치가 있는 사실만 KEEP. "
            "발화·질문·진행보고·형식구조물·일회성·미확정은 DROP. 애매하면 DROP.\n\n"
            "=== 분류 기준 문서 ===\n"
            f"{taxonomy}\n"
            "=== 분류 기준 문서 끝 ===\n\n"
            "출력 형식(엄수): 후보마다 한 줄, '<번호> KEEP' 또는 '<번호> DROP <한단어사유>'. "
            "그 외 설명·머리말 금지.\n\n"
            "후보 목록:\n"
            f"{numbered}\n"
        )
        try:
            proc = subprocess.run(
                ["claude", "-p", "--model", CONSOLIDATE_MODEL, prompt],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"rc={proc.returncode}: {proc.stderr.strip()}")
            verdicts = _parse_keepdrop(proc.stdout, len(batch))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
            print(f"  [warn] 2차 필터 실패(이 묶음 전부 KEEP 폴백): {e}", file=sys.stderr)
            filter_failed = True
            kept.extend(batch)
            continue

        for i, it in enumerate(batch, 1):
            verdict, reason = verdicts.get(i, ("DROP", "판정누락"))
            if verdict == "KEEP":
                kept.append(it)
            else:
                dropped.append((it, reason))

    return kept, dropped, filter_failed


def _backup_md(path):
    """대상 md 백업 <path>.bak.YYYYMMDD 생성. 경로 반환."""
    import shutil

    stamp = datetime.date.today().strftime("%Y%m%d")
    bak = path + ".bak." + stamp
    shutil.copy(path, bak)
    return bak


def _build_consolidate_report(new_items, skipped, had_error, mem_lines):
    """텔레그램 무음 리포트 텍스트 생성(유저친화·기술용어 금지·별표강조 금지).
    new_items: 적재된 신규 항목(메타 미부착 원문 한 줄들).
    skipped: 중복으로 넘긴 개수(리포트엔 안 쓰고 stdout 로그용으로만 받음).
    had_error: 정리 중 에러(임베딩/압축 실패 등) 있었는지.
    mem_lines: 현재 inbox.md 줄 수.
    반환: 발송할 리포트 문자열, 또는 None(발송 스킵).
    """
    md = datetime.date.today().strftime("%-m/%-d")
    n = len(new_items)

    # 신규 0건 & 에러 없음 & 크기경고 없음 → 조용히 발송 스킵(매일 노이즈 방지).
    over_cap = mem_lines > MEMORY_LINES_CAP
    if n == 0 and not had_error and not over_cap:
        return None

    lines = [f"🌙 자는 동안 기억 정리했어요 ({md})"]
    if n > 0:
        lines.append(f"- 새로 기억한 것 {n}가지")
        for it in new_items:
            lines.append(f"  · {it}")
    else:
        lines.append("- 새로 기억할 만한 건 없었어요")

    if over_cap:
        lines.append("받은편지함에 많이 쌓였어요 — 주제별로 정리할까요?")
    if had_error:
        lines.append("정리하다 걸린 데가 있었어요, 봐주세요")

    return "\n".join(lines)


def _send_silent_report(text):
    """dogany-proactive-push 의 push.sh 로 무음 발송. 성공 True.
    push.sh --silent --text <리포트> 호출."""
    if not CONSOLIDATE_REPORT_ENABLED:
        return False
    push_sh = os.path.normpath(os.path.join(HERE, "..", "routines", "push.sh"))
    if not os.path.isfile(push_sh):
        print(f"[warn] push.sh 없음: {push_sh}", file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            ["bash", push_sh, "--silent", "--text", text],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[warn] 리포트 발송 실패: {e}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"[warn] push.sh rc={proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


def cmd_consolidate(args):
    now = datetime.datetime.now(datetime.timezone.utc)

    conn = connect()
    init_db(conn)

    # 1) 워터마크 로드 (--since-days 면 무시하고 최근 N일 강제)
    if args.since_days is not None:
        watermark = (now - datetime.timedelta(days=args.since_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        print(f"[consolidate] --since-days {args.since_days} → 워터마크 무시, {watermark} 이후")
    else:
        watermark = _ts_load_watermark(conn)
        if watermark:
            print(f"[consolidate] 워터마크: {watermark} 이후")
        else:
            print(f"[consolidate] 워터마크 없음 → 최근 {DEFAULT_LOOKBACK_DAYS}일치")

    # 2) 트랜스크립트 증분 수집
    convo, max_ts, n_msgs = collect_transcript(watermark, now)
    if not convo:
        print("정리할 새 대화 없음.")
        conn.close()
        return 0
    print(f"[consolidate] 대화 {n_msgs}건 수집 (최대 ts={max_ts})")

    # 2.5) raw transcript archive (DGN-093): 워터마크 전진/프룬으로 버려지기 전에
    # 소비한 구간을 TEXT ONLY 로 gzip 월별 아카이브에 append. collect_transcript 와
    # 같은 소스/워터마크(_iter_transcript_rows)라 소비 구간을 넘겨 읽지 않는다.
    # dry-run 이면 원본 상태를 안 바꾸므로 아카이브도 생략.
    if not args.dry_run:
        n_arch = archive_raw_transcript(watermark, now)
        print(f"[consolidate] raw archive 적재 {n_arch}건 → {RAW_ARCHIVE_DIR}")

    # 3) 1차 압축 (Sonnet, 느슨하게 많이). 길면 청크 분할.
    candidates, compress_failed = compress_convo_chunked(convo)
    print(f"[consolidate] 1차 압축 후보 {len(candidates)}개")

    if not candidates:
        # 청크가 전부 압축 실패해 후보가 0이면 워터마크를 전진하지 않는다
        # (다음 밤에 같은 구간을 재시도해야 함). 단순히 기억할 게 없던 거면 전진.
        if compress_failed and compress_failed == len(_chunk_convo(convo)):
            print("모든 청크 압축 실패 — 워터마크 보존(다음 실행 재시도).")
            conn.close()
            if not args.dry_run and not args.no_push:
                rep = _build_consolidate_report([], 0, True, 0)
                if rep:
                    _send_silent_report(rep)
            return 1
        print("기억할 만한 영속 사실 없음.")
        if not args.dry_run:
            _ts_save_watermark(conn, max_ts)
            prune_transcript_fts(conn, max_ts)
        conn.close()
        return 0

    # 3.5) 룰필터(후처리, 결정론적 이중 안전망): 형식구조물/발화/명령 후보 제거.
    candidates, rule_dropped = _rule_filter_candidates(candidates)
    print(f"[consolidate] 룰필터 후 {len(candidates)}개 (룰로 {len(rule_dropped)}개 제거)")
    for it, why in rule_dropped:
        print(f"    [룰DROP/{why}] {it}")

    # 3.6) 2차 KEEP/DROP 필터(Sonnet + taxonomy). 묶음 판정.
    candidates, sec_dropped, filter_failed = _second_stage_filter(candidates)
    print(f"[consolidate] 2차 필터 후 {len(candidates)}개 (2차로 {len(sec_dropped)}개 DROP)")
    for it, why in sec_dropped:
        print(f"    [2차DROP/{why}] {it}")

    if not candidates:
        print("필터 통과한 영속 사실 없음.")
        if not args.dry_run:
            _ts_save_watermark(conn, max_ts)
            prune_transcript_fts(conn, max_ts)
        conn.close()
        return 0

    # 4) 중복제거: 각 후보 embed → 기존 notes 임베딩과 코사인. 최대 ≥ 임계면 스킵.
    existing_vecs = []
    embed_failed = False
    for row in conn.execute("SELECT embedding FROM notes WHERE embedding IS NOT NULL"):
        existing_vecs.append(blob_to_vec(row["embedding"]))

    new_items = []   # 신규(적재 대상) 원문 항목
    dup_items = []   # 중복으로 스킵한 항목
    for cand in candidates:
        if embed_failed:
            # 이미 임베딩이 깨졌으면 전부 신규 취급
            new_items.append(cand)
            continue
        try:
            cvec = embed(cand)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[warn] 임베딩 실패 — 중복제거 생략, 전부 신규 취급: {e}", file=sys.stderr)
            embed_failed = True
            new_items.append(cand)
            continue
        max_sim = 0.0
        for ev in existing_vecs:
            s = cosine(cvec, ev)
            if s > max_sim:
                max_sim = s
        if max_sim >= DEDUP_THRESHOLD:
            dup_items.append(cand)
        else:
            new_items.append(cand)
            existing_vecs.append(cvec)  # 후보 간 중복도 잡도록 풀에 추가

    print(f"[consolidate] 신규 {len(new_items)}개 / 중복 스킵 {len(dup_items)}개"
          + (" / 임베딩 실패(중복제거 생략)" if embed_failed else ""))

    # 5) 메타 부착
    today = datetime.date.today().isoformat()
    tagged = [f"{it} ({today}, 야간공고화)" for it in new_items]

    # 미리보기 출력(항상)
    print(f"\n== 적재 예정 ({len(tagged)}개) → inbox.md ==")
    for it in tagged:
        print(f"  § {it}")
    if dup_items:
        print(f"\n-- 이미 아는 것(스킵) {len(dup_items)}개 --")
        for it in dup_items:
            print(f"  ~ {it}")

    # dry-run: 파일/DB/워터마크/푸시 전부 건드리지 않음
    if args.dry_run:
        print("\n[dry-run] 파일·워터마크·재인덱스·푸시 모두 생략. 위가 미리보기.")
        conn.close()
        return 0

    # 5b) 적재 (신규 있을 때만 백업+append+재인덱스). inbox.md 는 § 기반 → has_sep 자동판정.
    if tagged:
        bak = _backup_md(TARGET_MEMORY_MD)
        print(f"[consolidate] 백업 생성: {os.path.basename(bak)}")
        where = append_notes_to_md(TARGET_MEMORY_MD, None, tagged)
        print(f"[consolidate] {where}")

    # 6) 워터마크 전진 + 당일 롤링 인덱스 프룬(증류 끝난 ts<=max_ts raw 삭제)
    _ts_save_watermark(conn, max_ts)
    pruned = prune_transcript_fts(conn, max_ts)
    print(f"[consolidate] 워터마크 전진 → {max_ts} (당일 raw {pruned}행 프룬)")
    conn.close()

    # 6b) raw archive 보존기간 프룬(DGN-093): ~365일 지난 월별 아카이브 삭제.
    n_rawpruned = prune_raw_archive(now)
    if n_rawpruned:
        print(f"[consolidate] raw archive 만료 {n_rawpruned}개 삭제")

    # 7) 재인덱싱 (신규 적재 있었을 때만 의미 있음)
    if tagged:
        print("[consolidate] 재인덱싱...")
        cmd_index(argparse.Namespace(lock=None))

    # 8) 리포트 생성 + 무음 발송
    try:
        mem_lines = sum(1 for _ in open(TARGET_MEMORY_MD, "r", encoding="utf-8"))
    except OSError:
        mem_lines = 0
    had_error = embed_failed or compress_failed > 0 or filter_failed
    report = _build_consolidate_report(new_items, len(dup_items), had_error, mem_lines)

    if report is None:
        print("[consolidate] 리포트 발송 스킵(신규 0·에러 없음·여유 충분).")
        return 0

    print("\n-- 리포트 --\n" + report)
    if args.no_push:
        print("\n[--no-push] 텔레그램 발송 생략.")
        return 0
    if _send_silent_report(report):
        print("[consolidate] 리포트 무음 발송 완료.")
    return 0


# ======================================================================
# classify-inbox 커맨드 (주간 inbox 분류: inbox.md → 주제파일 배분 + 새 주제 제안)
# ======================================================================
# 핵심: 야간 공고화가 inbox.md 에 단순 적재한 항목을, 주 1회 Opus 로 판정해
# 현존 주제파일(memories/*.md, USER·inbox 제외)로 배분한다.
# - 배분: 해당 주제파일에 append(파일 형식 has_sep 따라), inbox.md 에서 제거.
# - 새 주제: 생성하지 않고 리포트로만 제안(같은 새 주제 3개 이상일 때).
# - 버림: 가치 없는 항목은 로그만 남기고 inbox 에서 제거.
# 안전 최우선: 실패(Opus 한도/에러)하면 inbox.md 를 절대 손대지 않고 rc!=0 으로 종료.
CLASSIFY_MODEL = "opus"
# 분류 후보(주제) 파일에서 항상 제외할 것. inbox 는 소스, USER 는 hot.
CLASSIFY_EXCLUDE = {"USER.md", "inbox.md"}
# 같은 '새 주제군' 라벨이 이 개수 이상 모이면 "새 파일 만들까요?" 제안.
NEW_TOPIC_SUGGEST_MIN = 3

# classify-inbox 종료 코드(체크 래퍼와 계약).
#   0 = 실제 분류 성공(마커 찍어도 됨) / 2 = 분류할 항목 없음(마커 찍지 마라) /
#   1 = 실패(Opus 한도·에러, inbox 보존, 다음 실행 재시도).
# 핵심: 빈 inbox(헤더·주석시드·빈 § 만)를 "항목 있음"으로 오판해 rc=0 마커를 찍으면,
# 나중에 진짜 항목이 쌓여도 7일 스킵으로 영영 분류가 안 돈다 → 항목없음은 반드시 rc=2.
CLASSIFY_RC_OK = 0
CLASSIFY_RC_EMPTY = 2
CLASSIFY_RC_FAIL = 1


def inbox_has_items(path=None):
    """inbox.md 에 실제 분류 대상 항목이 하나라도 있는가?
    체크 래퍼와 cmd_classify_inbox 가 '빔' 판정을 공유하기 위한 단일 기준.
    헤더·주석시드·빈 구분자 등 비실항목은 _inbox_items 가 이미 걸러낸다."""
    if path is None:
        path = os.path.join(MEMORIES_DIR, "inbox.md")
    if not os.path.isfile(path):
        return False
    return len(_inbox_items(path)) > 0


def _inbox_items(path):
    """inbox.md 에서 실제 분류 대상 항목만 추출.
    § 기반 파일이므로 parse_markdown 으로 노트를 뽑되, 주석(HTML <!-- -->)·
    빈 시드는 제외. 반환: [(note_text, ...)] 형태가 아니라 순수 텍스트 리스트."""
    notes = parse_markdown(path)
    items = []
    for nt in notes:
        t = (nt.get("text") or "").strip()
        if not t:
            continue
        # HTML 주석 시드(<!-- ... -->)만으로 이뤄진 블록 제외
        stripped = re.sub(r"<!--.*?-->", "", t, flags=re.DOTALL).strip()
        if not stripped:
            continue
        items.append(stripped)
    return items


def _classify_topics(memories_dir):
    """현재 주제파일 목록을 동적 수집(하드코딩 금지).
    memories/*.md 중 CLASSIFY_EXCLUDE 제외. 각 파일의 첫 헤더(섹션명)도 같이.
    반환: [(filename, section_hint), ...]. 친구가 옵시디언으로 파일 추가해도 따라감."""
    topics = []
    for fname in sorted(os.listdir(memories_dir)):
        if not fname.endswith(".md") or fname in CLASSIFY_EXCLUDE:
            continue
        section_hint = ""
        try:
            with open(os.path.join(memories_dir, fname), "r", encoding="utf-8") as f:
                for ln in f:
                    if _is_header(ln):
                        section_hint = _header_text(ln)
                        break
        except OSError:
            pass
        topics.append((fname, section_hint))
    return topics


def _parse_classify_output(stdout, n_items, valid_files):
    """분류기 출력 파싱. 기대 형식: 줄당 '<번호> <판정>'.
    판정 = 주제파일명(예: about-user.md) | NEW:<라벨> | DROP.
    반환: {idx(1-base): ('FILE', fname) | ('NEW', label) | ('DROP', '')}.
    파싱 못하거나 알 수 없는 파일명이면 호출부에서 보수적으로 무시(inbox 잔류)."""
    verdicts = {}
    for ln in stdout.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^\[?(\d+)\]?[).:\s-]+\s*(.+)$", s)
        if not m:
            continue
        idx = int(m.group(1))
        if not (1 <= idx <= n_items):
            continue
        verdict = m.group(2).strip()
        up = verdict.upper()
        if up == "DROP":
            verdicts[idx] = ("DROP", "")
        elif up.startswith("NEW:") or up.startswith("NEW "):
            label = verdict[4:].strip().lstrip(":").strip()
            verdicts[idx] = ("NEW", label or "미상")
        else:
            # 파일명 판정. 확장자 보정.
            cand = verdict.split()[0].strip()
            if not cand.endswith(".md"):
                cand_md = cand + ".md"
            else:
                cand_md = cand
            if cand_md in valid_files:
                verdicts[idx] = ("FILE", cand_md)
            # 알 수 없는 파일명 → 무시(inbox 잔류, 안전)
    return verdicts


def _run_classifier(items, topics):
    """Opus 분류기 1회 호출. 각 inbox 항목을 주제파일/새주제/버림으로 판정.
    반환: (verdicts dict, ok bool). ok=False면 호출 실패(inbox 보존해야 함)."""
    topic_lines = "\n".join(
        f"- {fn}" + (f" (섹션: {sec})" if sec else "") for fn, sec in topics
    )
    numbered = "\n".join(f"{i}. {it}" for i, it in enumerate(items, 1))
    prompt = (
        "너는 생활비서 __AGENT_LABEL__의 장기기억 분류기다. 받은편지함(inbox)에 쌓인 항목들을 "
        "현존 주제파일 중 하나로 배분하거나, 새 주제군으로 표시하거나, 가치 없으면 버린다.\n\n"
        "현존 주제파일 목록(여기 있는 파일명으로만 배분 가능):\n"
        f"{topic_lines}\n\n"
        "판정 규칙:\n"
        "- 항목이 위 주제파일 중 하나에 자연스럽게 속하면 그 파일명을 그대로 출력(예: about-user.md).\n"
        "- 어느 파일에도 안 맞지만 장기 기억할 가치가 있는 새 주제군이면 'NEW:<짧은라벨>' 출력(예: NEW:건강기록). 파일을 새로 만들지는 않는다.\n"
        "- 한 달~일 년 뒤에 쓸모없을 일회성·발화·진행성 항목이면 'DROP' 출력.\n"
        "- 애매하면 가장 가까운 기존 파일로 배분(새 주제 남발 금지).\n\n"
        "출력 형식(엄수): 항목마다 한 줄, '<번호> <판정>'. 판정은 파일명 | NEW:<라벨> | DROP 중 하나. "
        "그 외 설명·머리말 절대 금지.\n\n"
        "받은편지함 항목:\n"
        f"{numbered}\n"
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", CLASSIFY_MODEL, prompt],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[error] 분류기 호출 실패: {e}", file=sys.stderr)
        return {}, False
    if proc.returncode != 0:
        print(f"[error] 분류기 비정상 종료(rc={proc.returncode}): {proc.stderr.strip()}",
              file=sys.stderr)
        return {}, False

    valid_files = {fn for fn, _ in topics}
    verdicts = _parse_classify_output(proc.stdout, len(items), valid_files)
    return verdicts, True


def _rewrite_inbox(path, keep_items):
    """inbox.md 를 keep_items 만 남기고 재작성(원자교체).
    헤더(### 미분류 (inbox))와 주석 시드는 보존하고, § 블록 본문만 교체.
    keep_items 가 비면 시드 주석만 남는 빈 inbox 로 복원."""
    # 원본에서 헤더 라인과 주석 시드를 추출(첫 헤더 + HTML 주석 블록).
    with open(path, "r", encoding="utf-8") as f:
        orig = f.read()

    header_line = "### 미분류 (inbox)"
    for ln in orig.splitlines():
        if _is_header(ln):
            header_line = ln.rstrip()
            break

    # 주석 시드(<!-- ... -->) 보존: 원본에서 그대로 떼어온다.
    seed_comment = ""
    mcomment = re.search(r"<!--.*?-->", orig, flags=re.DOTALL)
    if mcomment:
        seed_comment = mcomment.group(0)

    parts = [header_line, "§"]
    if seed_comment:
        parts.append(seed_comment)
        parts.append("§")
    for it in keep_items:
        parts.append(it)
        parts.append("§")
    # 끝의 잉여 § 정리: 마지막이 § 면 그대로 둬도 parse 안전(빈 블록 무시).
    new_content = "\n".join(parts) + "\n"

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.replace(tmp, path)


def cmd_classify_inbox(args):
    """주간 inbox 분류. 종료코드 계약: 성공=0 / 항목없음=2 / 실패=1.
    실패하면 inbox.md 를 절대 손대지 않고 rc=1(다음 실행 재시도).
    '항목없음'은 rc=2 — 체크 래퍼가 마커를 안 찍게 해 진짜 항목이 올 때까지 매일 본다."""
    inbox_path = os.path.join(MEMORIES_DIR, "inbox.md")
    if not os.path.isfile(inbox_path):
        print("[classify-inbox] inbox.md 없음 — 할 일 없음(rc=2).")
        return CLASSIFY_RC_EMPTY

    items = _inbox_items(inbox_path)
    if not items:
        print("[classify-inbox] inbox 비어 있음(헤더·주석·빈§만) — 종료(작업 없음, rc=2).")
        return CLASSIFY_RC_EMPTY
    print(f"[classify-inbox] inbox 항목 {len(items)}개")

    topics = _classify_topics(MEMORIES_DIR)
    if not topics:
        print("[error] 배분할 주제파일이 하나도 없음 — inbox 보존, 비정상 종료(rc=1).",
              file=sys.stderr)
        return CLASSIFY_RC_FAIL
    print(f"[classify-inbox] 주제 후보 {len(topics)}개: " +
          ", ".join(fn for fn, _ in topics))

    # Opus 분류 1회
    verdicts, ok = _run_classifier(items, topics)
    if not ok:
        # 호출 실패: inbox 절대 손대지 않고 비정상 종료(다음 실행 재시도).
        print("[error] 분류기 실패 — inbox.md 보존, 비정상 종료(rc=1).", file=sys.stderr)
        return CLASSIFY_RC_FAIL

    # 판정 집계
    assign = {}     # fname -> [item, ...]
    new_groups = {} # label -> [item, ...]
    dropped = []    # [item, ...]
    keep_in_inbox = []  # 판정 누락/알수없음 → inbox 잔류(안전)
    for i, it in enumerate(items, 1):
        v = verdicts.get(i)
        if not v:
            keep_in_inbox.append(it)
            continue
        kind, payload = v
        if kind == "FILE":
            assign.setdefault(payload, []).append(it)
        elif kind == "NEW":
            new_groups.setdefault(payload, []).append(it)
            keep_in_inbox.append(it)  # 새 주제는 파일 안 만드니 inbox 에 남김
        elif kind == "DROP":
            dropped.append(it)

    # 미리보기 출력
    print("\n== 분류 결과 ==")
    for fn, lst in assign.items():
        print(f"  [→ {fn}] {len(lst)}개")
        for it in lst:
            print(f"      · {it}")
    if new_groups:
        print("  [새 주제 후보]")
        for label, lst in new_groups.items():
            mark = " ★제안" if len(lst) >= NEW_TOPIC_SUGGEST_MIN else ""
            print(f"    NEW:{label} ({len(lst)}개){mark}")
            for it in lst:
                print(f"      · {it}")
    if dropped:
        print(f"  [버림] {len(dropped)}개")
        for it in dropped:
            print(f"      · {it}")
    if keep_in_inbox:
        print(f"  [inbox 잔류] {len(keep_in_inbox)}개 (새주제/판정누락)")

    if args.dry_run:
        print("\n[dry-run] 파일·inbox·재인덱스 모두 미실행. 위가 미리보기.")
        return CLASSIFY_RC_OK

    # 실제 적재: 주제파일에 append(파일 형식 has_sep 자동판정).
    today = datetime.date.today().isoformat()
    if assign:
        bak = _backup_md(inbox_path)
        print(f"[classify-inbox] inbox 백업: {os.path.basename(bak)}")
        for fn, lst in assign.items():
            target = os.path.join(MEMORIES_DIR, fn)
            if not os.path.isfile(target):
                # 분류 중 파일이 사라졌으면(레이스) inbox 에 되돌림(안전).
                keep_in_inbox.extend(lst)
                print(f"  [warn] 대상 파일 사라짐 {fn} — inbox 잔류로 되돌림")
                continue
            tagged = [f"{it} ({today}, inbox분류)" for it in lst]
            where = append_notes_to_md(target, None, tagged)
            print(f"  [적재] {fn}: {len(lst)}개 — {where}")

    # inbox 재작성: 잔류 항목만 남김(배분·버림 제거).
    _rewrite_inbox(inbox_path, keep_in_inbox)
    print(f"[classify-inbox] inbox 재작성: 잔류 {len(keep_in_inbox)}개")

    # 새 주제 제안(3개 이상 모인 라벨만)
    suggestions = [label for label, lst in new_groups.items()
                   if len(lst) >= NEW_TOPIC_SUGGEST_MIN]

    # 재인덱싱(배분으로 파일들 바뀜)
    print("[classify-inbox] 재인덱싱...")
    cmd_index(argparse.Namespace(lock=None))

    # 리포트(있을 때만 무음 발송)
    report = _build_classify_report(assign, suggestions, dropped, today)
    if report:
        print("\n-- 리포트 --\n" + report)
        if not args.no_push:
            if _send_silent_report(report):
                print("[classify-inbox] 리포트 무음 발송 완료.")

    return CLASSIFY_RC_OK


def cmd_inbox_count(args):
    """inbox.md 의 실제 분류 대상 항목 수를 출력(체크 래퍼와 '빔' 판정 공유).
    stdout 에 정수 한 줄. 종료코드: 항목 있으면 0, 없으면 2(빈 inbox).
    체크 래퍼는 이 rc 만 보고 classify-inbox 호출 여부를 정한다 — 양쪽이
    동일한 _inbox_items 기준을 쓰므로 '실항목 있다 판정 → classify 는 빔' 불일치가 사라진다."""
    inbox_path = os.path.join(MEMORIES_DIR, "inbox.md")
    n = len(_inbox_items(inbox_path)) if os.path.isfile(inbox_path) else 0
    print(n)
    return CLASSIFY_RC_OK if n > 0 else CLASSIFY_RC_EMPTY


def _build_classify_report(assign, suggestions, dropped, today):
    """주간 분류 리포트(유저친화·별표강조 금지). 변화 없으면 None."""
    n_assigned = sum(len(v) for v in assign.values())
    if n_assigned == 0 and not suggestions:
        return None
    md = datetime.date.today().strftime("%-m/%-d")
    lines = [f"🗂️ 받은편지함 정리했어요 ({md})"]
    if n_assigned:
        lines.append(f"- 주제별로 {n_assigned}가지 분류")
        for fn, lst in assign.items():
            lines.append(f"  · {fn.replace('.md','')}: {len(lst)}개")
    if dropped:
        lines.append(f"- 안 쓸 것 {len(dropped)}가지 정리")
    if suggestions:
        lines.append("- 새 주제가 모였어요, 새 파일 만들까요? → " + ", ".join(suggestions))
    return "\n".join(lines)


# ======================================================================
# hook 커맨드 (UserPromptSubmit 자동주입) — inject_hook.py 로직 이식
# ======================================================================
# 설계 원칙(절대 깨지 말 것):
#   1. 에이전트 턴을 절대 막지 않는다. 어떤 에러도 조용히 exit 0 + 빈 출력.
#   2. 빠르다. 매 메시지마다 도니까 임베딩에 짧은 타임아웃을 걸고,
#      전체 검색을 스레드 타임아웃으로 한 번 더 감싼다.
#   3. 약한 매칭은 억지로 주입하지 않는다. top1 cosine < 컷오프면 빈손.
# 출력 규약: JSON {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
#   "additionalContext": ...}} — system reminder로 감싸져 채팅에 노출 안 됨.
HOOK_TOPK = 4              # 검색 상위 N
HOOK_VEC_CUTOFF = 0.50    # top1 cosine이 이 미만이면 주입 생략(보수적)
HOOK_EMBED_TIMEOUT = 6    # Ollama 임베딩 urlopen 타임아웃(초)
HOOK_SEARCH_TIMEOUT = 9   # 검색 전체 스레드 타임아웃(초)
HOOK_SNIPPET_MAX = 400    # 결과 본문 발췌 길이 상한
HOOK_STALE_LOCK_SEC = 600  # 이보다 오래된 락은 죽은 프로세스 잔재로 보고 무시


def _now_local_line():
    """현재 시각 한 줄(사용자 맥의 OS 로컬 시간대 기준). 매 메시지 자동주입 맨 앞에 박아
    에이전트 '시간맹'을 막는다. Dogany는 글로벌 배포 → KST 하드코딩 금지, OS TZ를 따라간다.
    표준 라이브러리만 사용(의존성 추가 금지).
    now().astimezone()으로 OS 로컬 TZ aware datetime, tzname()으로 약어 자동(KST/PST/EST 등).
    형식: [현재 시각] 2026-06-26 (금) 09:36 KST (약어는 시스템 TZ에 따라 치환)."""
    now = datetime.datetime.now().astimezone()
    weekday = "월화수목금토일"[now.weekday()]
    tz = now.tzname() or ""
    suffix = f" {tz}" if tz else ""
    return f"[현재 시각] {now.strftime('%Y-%m-%d')} ({weekday}) {now.strftime('%H:%M')}{suffix}"


def _hook_body_state_line():
    """현재 신체/목표 상태 한 줄(v2 결정론적 주입). lifekit 있으면 값, 없으면 None.

    핵심: 이 주입은 검색/주제분류/에이전트 판단에 의존하지 않는다. lifekit(config 테이블)이
    붙어 있으면 매 턴 무조건 body-state 한 줄을 넣어, "묻기 전에 읽는" 규칙을 코드로 못 박는다.
    metal/template 처럼 lifekit 이 없으면(import 실패 또는 body-state 미구성) None → no-op.
    싼 SQLite read 1회. 어떤 예외도 삼켜 턴을 막지 않는다(best effort)."""
    try:
        # lifekit 는 레포 루트의 database/ 에 있다(path-independent).
        # 우선순위: 1) LIFEKIT_DIR 환경변수, 2) PROJECT_ROOT/database, 3) HERE/../database.
        # 어느 쪽도 없으면(lifekit 미탑재 또는 body-state 미구성) 조용히 no-op.
        env_dir = os.environ.get("LIFEKIT_DIR")
        proj_root = os.environ.get("PROJECT_ROOT")
        candidates = []
        if env_dir:
            candidates.append(env_dir)
        if proj_root:
            candidates.append(os.path.join(proj_root, "database"))
        candidates.append(os.path.normpath(os.path.join(HERE, "..", "database")))
        db_dir = None
        for cand in candidates:
            # lifekit.py 뿐 아니라 실제 데이터(lifekit.db)까지 있어야 body-state 를 읽는다.
            # 코드만 있고 db 가 없으면(신규 클론) 조용히 no-op — stderr 잡음도 없다.
            if os.path.isfile(os.path.join(cand, "lifekit.py")) and \
               os.path.isfile(os.path.join(cand, "lifekit.db")):
                db_dir = cand
                break
        if db_dir is None:
            return None  # lifekit 없음/미구성 → 조용히 no-op
        if db_dir not in sys.path:
            sys.path.insert(0, db_dir)
        import lifekit as _lk
        # Fresh mint: db exists but the config table is empty (no real user data
        # yet). load_body_stats() would fall back to DEFAULT_STATS (a generic
        # placeholder body, NOT the owner's) -- injecting that would fake data.
        # So: no config rows -> no-op. The line appears only once the user sets
        # real stats via the lifekit CLI (config table gets rows).
        try:
            conn = _lk.get_conn()
            try:
                n = conn.execute("SELECT COUNT(*) FROM config;").fetchone()[0]
            finally:
                conn.close()
        except BaseException:
            return None
        if not n:
            return None  # empty config -> new user -> no body-state line
        stats = _lk.load_body_stats()
        t = _lk.compute_targets(stats, exercise_kcal=0)
        g = _lk.compute_macro_goals(t["eff_goal"], stats)
        gm = stats.get("goal_mode", "")
        wt = stats.get("weight_kg", "")
        # 숫자 float 잔재(84.0)를 사람이 읽기 좋게 정수로(정수값이면). 값 계산엔 영향 없음.
        def _n(x):
            try:
                fx = float(x)
                return int(fx) if fx == int(fx) else fx
            except (TypeError, ValueError):
                return x
        return (
            f"[현재 신체/목표] goal_mode={gm} weight={_n(wt)} "
            f"eff_goal={t['eff_goal']} protein={_n(g['protein'])} "
            f"carb={_n(g['carb'])} fat={_n(g['fat'])} "
            "(lifekit canonical; 사용자께 다시 묻지 말 것)"
        )
    except BaseException:
        return None  # lifekit 미구성/에러 → no-op(턴 방해 금지)


def _hook_compose(recall_ctx=None):
    """주입할 additionalContext 문자열을 조립(항상 시각 + 있으면 body-state + 있으면 recall).
    body-state 는 v2 결정론적 주입(매 턴). recall 은 약한매칭 컷을 통과한 것만."""
    parts = [_now_local_line()]
    bs = _hook_body_state_line()
    if bs:
        parts.append(bs)
    if recall_ctx:
        parts.append(recall_ctx)
    return "\n\n".join(parts)


def _emit_empty():
    """매칭 없어도 현재 시각 한 줄 + (있으면) body-state 를 무조건 주입 + 성공.
    body-state 는 v2: lifekit 있으면 검색 결과와 무관하게 매 턴 결정론적 주입."""
    try:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _hook_compose(None),
            }
        }
        print(json.dumps(out, ensure_ascii=False), flush=True)
    except Exception:
        pass  # 시각 주입 실패해도 턴을 막지 않는다(빈손 폴백).
    sys.exit(0)


def _short_embed(text):
    """embed()와 동일 로직이되 urlopen 타임아웃만 짧게(hook 경로 전용).
    모듈 전역 embed를 이 함수로 monkeypatch 해 hook에서만 짧은 타임아웃 강제."""
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=HOOK_EMBED_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding")
    if not vec or not isinstance(vec, list):
        raise ValueError("Ollama 응답에 embedding 없음")
    return vec


def _hook_is_stale():
    """소스(memories/*.md) 최신 mtime > state.db mtime 이면 stale.
    db가 없으면 stale(최초 빌드). 소스가 하나도 없으면 not stale."""
    if not os.path.isdir(MEMORIES_DIR):
        return False
    src_mtime = 0.0
    found = False
    for name in os.listdir(MEMORIES_DIR):
        if not name.endswith(".md") or name in INDEX_EXCLUDE:
            continue
        try:
            src_mtime = max(src_mtime, os.path.getmtime(os.path.join(MEMORIES_DIR, name)))
            found = True
        except OSError:
            continue
    if not found:
        return False
    try:
        db_mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return True  # db 없음 → 빌드 필요
    return src_mtime > db_mtime


def _hook_acquire_lock(lock_path):
    """원자적 락 획득(O_CREAT|O_EXCL). 성공 True, 이미 있으면 False.
    단 stale(HOOK_STALE_LOCK_SEC 초과) 락이면 제거 후 재시도."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            age = 0
        if age > HOOK_STALE_LOCK_SEC:
            try:
                os.remove(lock_path)
            except OSError:
                pass
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except OSError:
                return False
        return False


def _hook_maybe_reindex():
    """소스가 state.db보다 최신이면 백그라운드 detach로 재인덱싱을 던진다.
    이번 턴 검색은 기존(낡은) 인덱스로 그대로 진행(절대 기다리지 않음).
    실패해도 조용히 넘어간다(best effort)."""
    if not _hook_is_stale():
        return
    lock_path = os.path.join(HERE, ".reindex.lock")
    if not _hook_acquire_lock(lock_path):
        return  # 이미 재인덱싱 진행 중

    cmd = [sys.executable, os.path.abspath(__file__), "index", "--lock", lock_path]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=HERE,
        )
    except Exception:
        # Popen 실패 시 우리가 잡은 락은 풀어준다(영구 차단 방지)
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _hook_search_with_timeout(query):
    """검색 전체를 데몬 스레드로 돌려 HOOK_SEARCH_TIMEOUT 초 내 결과만 받는다.
    타임아웃/에러 시 None."""
    import threading

    box = {}

    def worker():
        try:
            box["result"] = search_core(query, k=HOOK_TOPK, log=False)
        except BaseException as e:  # SystemExit 포함 전부 흡수
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(HOOK_SEARCH_TIMEOUT)
    if t.is_alive():
        return None  # 타임아웃 → 포기
    return box.get("result")


def _hook_build_context(results):
    """주입할 additionalContext 문자열. 약한 매칭이면 None.
    컷: cosine 키 < HOOK_VEC_CUTOFF 제외. cosine None(FTS만 매치)도 보수적 제외."""
    if not results:
        return None
    top = results[0].get("cosine")
    if top is None or top < HOOK_VEC_CUTOFF:
        return None

    lines = ["[관련 기억 — 자동검색]"]
    for r in results:
        cs = r.get("cosine")
        if cs is None or cs < HOOK_VEC_CUTOFF:
            continue
        section = r.get("section") or "(no section)"
        text = (r.get("text") or "").strip().replace("\n", " ")
        if len(text) > HOOK_SNIPPET_MAX:
            text = text[:HOOK_SNIPPET_MAX] + "…"
        lines.append(f"- [{section}] {text}")
    if len(lines) == 1:  # 헤더만 남음 → 실제 항목 없음
        return None
    return "\n".join(lines)


def cmd_hook(args):
    """UserPromptSubmit hook. 내부에서 직접 exit(0) 처리(main rc에 의존 안 함).
    어떤 에러든 stdout에 아무것도 안 쓰고 exit 0(빈손폴백)."""
    try:
        # 1) stdin JSON 파싱 → prompt 추출
        try:
            raw = sys.stdin.read()
            data = json.loads(raw)
            prompt = data.get("prompt")
        except Exception:
            _emit_empty()
            return
        if not isinstance(prompt, str) or not prompt.strip():
            _emit_empty()
            return

        # 1.5) 자동 재인덱싱 트리거(best effort, fire-and-forget).
        try:
            _hook_maybe_reindex()
        except Exception:
            pass

        # 2) hook 경로 전용 짧은 타임아웃 임베딩으로 전역 embed 재바인딩.
        global embed
        embed = _short_embed

        # 3) 검색(스레드 타임아웃 보호)
        results = _hook_search_with_timeout(prompt.strip())
        if not results:
            _emit_empty()
            return

        # 4) 컨텍스트 구성 + 약한매칭 컷
        ctx = _hook_build_context(results)
        if not ctx:
            _emit_empty()
            return

        # 5) JSON으로 주입(stdout에 1회만). 시각 + body-state(있으면) + recall 순.
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _hook_compose(ctx),
            }
        }
        print(json.dumps(out, ensure_ascii=False), flush=True)
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException:
        # 최후의 그물: 무슨 일이 있어도 턴을 막지 않는다.
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


# ======================================================================
# CLI
# ======================================================================
def main():
    p = argparse.ArgumentParser(description="__AGENT_LABEL__ 장기기억 회상 코어")
    sub = p.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("index", help="memories/*.md → state.db 적재 (증분)")
    ip.add_argument("--lock", default=None,
                    help="락 파일 경로(자동 재인덱싱용). 끝나면 atexit으로 해제.")

    sub.add_parser("hook", help="UserPromptSubmit hook(stdin JSON → 관련기억 자동주입)")

    sp = sub.add_parser("search", help="하이브리드 검색 (FTS5 + 벡터 RRF)")
    sp.add_argument("query")
    sp.add_argument("--k", type=int, default=5, help="결과 개수 (기본 5)")
    sp.add_argument("--json", action="store_true", help="JSON 출력")

    sub.add_parser("stats", help="인덱스/검색 통계")

    wp = sub.add_parser("write", help="원시 텍스트 → Haiku 압축 → md 적재 → 재인덱스")
    wp.add_argument("text", nargs="?", default=None, help="원시 텍스트 (생략 시 stdin)")
    wp.add_argument("--source", default=None, help='출처 라벨 (예: "텔레그램 대화")')
    wp.add_argument("--file", default="inbox.md", help="적재 대상 md (기본 inbox.md — 주제 명확하면 identity/work-rules/routines/infra/about-user.md 지정)")
    wp.add_argument("--section", default=None, help="적재할 섹션 헤더 (없으면 생성/파일끝)")
    wp.add_argument("--dry-run", action="store_true", help="압축 결과만 보고 파일 미수정")

    cp = sub.add_parser("consolidate", help="야간 공고화: 대화 트랜스크립트 → inbox.md 증류")
    cp.add_argument("--dry-run", action="store_true",
                    help="압축·중복제거까지만, 파일/워터마크/재인덱스/푸시 미실행(미리보기)")
    cp.add_argument("--no-push", action="store_true", help="적재는 하되 텔레그램 발송 안 함")
    cp.add_argument("--since-days", type=int, default=None,
                    help="워터마크 무시하고 최근 N일 강제(테스트용)")

    xp = sub.add_parser("classify-inbox",
                        help="주간 inbox 분류: inbox.md 항목을 주제파일로 배분 + 새주제 제안")
    xp.add_argument("--dry-run", action="store_true",
                    help="분류 판정만 미리보기, inbox/주제파일/재인덱스 미실행")
    xp.add_argument("--no-push", action="store_true", help="분류는 하되 텔레그램 발송 안 함")

    sub.add_parser("inbox-count",
                   help="inbox.md 실항목 수 출력(체크 래퍼와 빔 판정 공유). rc: 있음0/없음2")

    args = p.parse_args()

    if args.cmd == "hook":
        # cmd_hook은 내부에서 직접 exit(0). main rc에 의존하지 않는다.
        cmd_hook(args)
        return 0
    if args.cmd == "index":
        return cmd_index(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "write":
        return cmd_write(args)
    if args.cmd == "consolidate":
        return cmd_consolidate(args)
    if args.cmd == "classify-inbox":
        return cmd_classify_inbox(args)
    if args.cmd == "inbox-count":
        return cmd_inbox_count(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
