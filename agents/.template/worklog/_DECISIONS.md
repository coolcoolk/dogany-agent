# _DECISIONS -- 유저 판단 대기 원장 (DGN-214)
#
# 메인 세션이 유저에게 결정을 물을 때 한 줄 추가하고, 답이 오면 그 줄을
# 지운다. status-footer.py(Stop 훅)가 이 파일을 읽어 모든 응답 풋터에
# [결정대기]로 부착한다 -- 어떤 메시지를 봐도 맨 아래에 전체 대기 목록.
#
# 형식: "- [YYYY-MM-DD] [dec-NNN] <한 줄 요약>" (dec-NNN = 영구 ID, 인스턴스
#        단조증가 발번. 콘솔 딥링크 /decision/dec-NNN 이 이 ID를 쓴다. 상세가
#        필요한 결정은 worklog/decisions/dec-NNN.md 에 상세를 쓴다.)
# 규칙: 줄 추가와 동시에 worklog/decisions/dec-NNN.md 상세 파일을 같은
# 편집에서 만든다 -- 상세 없는 줄 등록 금지(콘솔 상세 뷰가 이 파일을 렌더링한다).
#
# 표기 규칙: dec-*.md 상세 파일 본문은 인스턴스 작업 언어로 쓴다 -- 콘솔에서
# 유저가 직접 읽는 유저-facing 표면이다.
